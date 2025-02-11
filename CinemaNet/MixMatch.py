## SOURCE --> https://github.com/oguiza/fastai_extensions/blob/master/fastai_extensions/exp/nb_MixMatch.py
## https://github.com/oguiza/fastai_extensions/blob/master/04a_MixMatch_extended.ipynb
## This script was written by Ignacio Oguiza, who in turn took heavy inspiration from Noah Rubinstein
## One tiny modification has been made
## * added `size` argument to specify unlabelled data image size in `def mixmatch(...)`


from fastai.vision import *
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="torch.nn.functional")
device = 'cuda' if torch.cuda.is_available() else 'cpu'


from numbers import Integral

class MultiTfmLabelList(LabelList):
    def __init__(self, x:ItemList, y:ItemList, tfms:TfmList=None, tfm_y:bool=False, K=2, **kwargs):
        "K: number of transformed samples generated per item"
        self.x,self.y,self.tfm_y,self.K = x,y,tfm_y,K
        self.y.x = x
        self.item=None
        self.transform(tfms, **kwargs)

    def __getitem__(self,idxs:Union[int, np.ndarray])->'LabelList':
        "return a single (x, y) if `idxs` is an integer or a new `LabelList` object if `idxs` is a range."
        idxs = try_int(idxs)
        if isinstance(idxs, Integral):
            if self.item is None: x,y = self.x[idxs],self.y[idxs]
            else:                 x,y = self.item   ,0
            if self.tfms or self.tfmargs:
                x = [x.apply_tfms(self.tfms, **self.tfmargs) for _ in range(self.K)]
            if hasattr(self, 'tfms_y') and self.tfm_y and self.item is None:
                y = y.apply_tfms(self.tfms_y, **{**self.tfmargs_y, 'do_resolve':False})
            if y is None: y=0
            return x,y
        else: return self.new(self.x[idxs], self.y[idxs])

def MultiCollate(batch):
    batch = to_data(batch)
    if isinstance(batch[0][0],list): batch = [[torch.stack(s[0]),s[1]] for s in batch]
    return torch.utils.data.dataloader.default_collate(batch)


def random_strat_splitter(y, train_size:int=1, seed:int=1):
    from sklearn.model_selection import StratifiedShuffleSplit
    sss = StratifiedShuffleSplit(n_splits=1, train_size=train_size, random_state=seed)
    idx = list(sss.split(np.arange(len(y)), y))[0]
    return idx[0],idx[1]


def _mixup(x1, y1, x2, y2, α=.75):
    β = np.random.beta(α, α)
    β = max(β, 1 - β)
    x = β * x1 + (1 - β) * x2
    y = β * y1 + (1 - β) * y2
    return x, y

def sharpen(x, T=0.5):
    p_target = x**(1. / T)
    return p_target / p_target.sum(dim=1, keepdims=True)


def drop_cb_fn(learn, cb_name:str)->None:
    cbs = []
    for cb in learn.callback_fns:
        if isinstance(cb, functools.partial): cbn = cb.func.__name__
        else: cbn = cb.__name__
        if cbn != cb_name: cbs.append(cb)
    learn.callback_fns = cbs


class MatchMixLoss(Module):
    "Adapt the loss function `crit` to go with MatchMix."

    def __init__(self, crit=None, reduction='mean', λ=100):
        super().__init__()
        if crit is None: crit = nn.CrossEntropyLoss()
        if hasattr(crit, 'reduction'):
            self.crit = crit
            self.old_red = crit.reduction
            setattr(self.crit, 'reduction', 'none')
        else:
            self.crit = partial(crit, reduction='none')
            self.old_crit = crit
        self.reduction = reduction
        self.λ = λ

    def forward(self, preds, target, bs=None):

        if bs is None: return F.cross_entropy(preds, target)

        labeled_preds = torch.log_softmax(preds[:bs],dim=1)
        Lx = -(labeled_preds * target[:bs]).sum(dim=1).mean()
        self.Lx = Lx.item()

        unlabeled_preds = torch.softmax(preds[bs:],dim=1)
        Lu = F.mse_loss(unlabeled_preds,target[bs:])
        self.Lu = (Lu * self.λ).item()

        return Lx + Lu * self.λ

    def get_old(self):
        if hasattr(self, 'old_crit'):  return self.old_crit
        elif hasattr(self, 'old_red'):
            setattr(self.crit, 'reduction', self.old_red)
            return self.crit


class MixMatchCallback(LearnerCallback):
    _order = -20

    def __init__(self,
                 learn: Learner,
                 labeled_data: DataBunch,
                 T: float = .5,
                 K: int = 2,
                 α: float = .75,
                 λ: float = 100):
        super().__init__(learn)

        self.learn, self.T, self.K, self.α, self.λ = learn, T, K, α, λ
        self.labeled_dl = labeled_data.train_dl
        self.n_classes = labeled_data.c
        self.labeled_data = labeled_data


    def on_train_begin(self, n_epochs, **kwargs):
        self.learn.loss_func = MatchMixLoss(crit=self.learn.loss_func, λ=self.λ)
        self.ldliter = iter(self.labeled_dl)
        self.smoothLx, self.smoothLu = SmoothenValue(0.98), SmoothenValue(0.98)
        self.recorder.add_metric_names(["train_Lx", "train_Lu*λ"])
        self.it = 0
        print('labeled dataset     : {:13,} samples'.format(len(self.labeled_data.train_ds)))
        print('unlabeled dataset   : {:13,} samples'.format(len(self.learn.data.train_ds)))
        total_samples = n_epochs *len(self.learn.data.train_dl) *\
        self.learn.data.train_dl.batch_size * (self.K + 1)
        print('total train samples : {:13,} samples'.format(total_samples))

    def on_batch_begin(self, last_input, last_target, train, **kwargs):
        if not train: return
        try:
            Xx, Xy = next(self.ldliter)             # Xx already augmented
        except StopIteration:
            self.ldliter = iter(self.labeled_dl)
            Xx, Xy = next(self.ldliter)             # Xx already augmented

        # LABELED
        bs = len(Xx)
        pb = torch.eye(self.n_classes)[Xy].to(device)

        # UNLABELED
        shape = list(last_input.size()[2:])
        Ux = last_input.view([-1] + shape)           # Ux already augmented (K items)
        with torch.no_grad():
            Uy = sharpen(torch.softmax(torch.stack([
                self.learn.model(last_input[:, i])
                for i in range(last_input.shape[1])],dim=1),dim=2).mean(dim=1),T=self.T)
        qb = Uy.repeat(1, 2).view((-1, Uy.size(-1)))

        #MIX
        Wx = torch.cat((Xx, Ux), dim=0)
        Wy = torch.cat((pb, qb), dim=0)
        shuffle = torch.randperm(Wx.shape[0])
        mixed_input, mixed_target = _mixup(Wx, Wy, Wx[shuffle], Wy[shuffle], α=self.α)

        return {"last_input": mixed_input, "last_target": (mixed_target, bs)}

    def on_batch_end(self, train, **kwargs):
        if not train: return
        self.smoothLx.add_value(self.learn.loss_func.Lx)
        self.smoothLu.add_value(self.learn.loss_func.Lu)
        self.it += 1

    def on_epoch_end(self, last_metrics, **kwargs):
        return add_metrics(last_metrics, [self.smoothLx.smooth, self.smoothLu.smooth])

    def on_train_end(self, **kwargs):
        """At the end of training, loss_func and data are returned to their original values,
        and this calleback is removed"""
        self.learn.loss_func = self.learn.loss_func.get_old()
        self.learn.data = self.labeled_data
        drop_cb_fn(self.learn, 'MixMatchCallback')


def mixmatch(learn: Learner, ulist: ItemList, num_workers:int=None, size:Union[int,tuple]=64,
             K: int = 2, T: float = .5, α: float = .75, λ: float = 100) -> Learner:

    labeled_data = learn.data
    if num_workers is None: num_workers = 1
    labeled_data.train_dl.num_workers = num_workers
    bs = labeled_data.train_dl.batch_size
    tfms = [labeled_data.train_ds.tfms, labeled_data.valid_ds.tfms]

    ulist = ulist.split_none()
    ulist.train._label_list = partial(MultiTfmLabelList, K=K)
    train_ul = ulist.label_empty().train           # Train unlabeled Labelist
    valid_ll = learn.data.label_list.valid         # Valid labeled Labelist
    udata = (LabelLists('.', train_ul, valid_ll)
             .transform(tfms, size=size)
             .databunch(bs=min(bs, len(train_ul)),val_bs=min(bs * 2, len(valid_ll)),
                        num_workers=num_workers,dl_tfms=learn.data.dl_tfms,device=device,
                        collate_fn=MultiCollate)
             .normalize(learn.data.stats))
    learn.data = udata
    learn.callback_fns.append(partial(MixMatchCallback, labeled_data=labeled_data, T=T, K=K, α=α, λ=λ))
    return learn

Learner.mixmatch = mixmatch