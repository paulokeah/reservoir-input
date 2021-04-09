
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

import pdb

import random

from utils import load_rb

def sigmoid(x):
    return 1/(1 + np.exp(-x))

def get_optimizer(args, train_params):
    op = None
    if args.optimizer == 'adam':
        op = optim.Adam(train_params, lr=args.lr, weight_decay=args.l2_reg)
    elif args.optimizer == 'sgd':
        op = optim.SGD(train_params, lr=args.lr, weight_decay=args.l2_reg)
    elif args.optimizer == 'rmsprop':
        op = optim.RMSprop(train_params, lr=args.lr, weight_decay=args.l2_reg)
    elif args.optimizer == 'lbfgs-pytorch':
        op = optim.LBFGS(train_params, lr=0.75)
    return op

def get_scheduler(args, op):
    if args.s_rate is not None:
        return optim.lr_scheduler.MultiStepLR(op, milestones=[1,2,3], gamma=args.s_rate)
    return None

# dataset that automatically creates trials composed of trial and context data
class TrialDataset(Dataset):
    def __init__(self, datasets, args):
        self.datasets = datasets
        self.args = args
        # arrays of just the context cues
        self.x_ctxs = []
        # cumulative lengths of datasets
        self.max_idxs = np.zeros(len(datasets), dtype=int)
        for i, ds in enumerate(datasets):
            x_ctx = np.zeros((args.T, ds[0].t_len))
            # setting context cue for appropriate task
            x_ctx[i] = 1
            # this is for transient context cue
            # x_ctx[i,10:20] = 1
            self.x_ctxs.append(x_ctx)
            self.max_idxs[i] = self.max_idxs[i-1] + len(ds)

    def __len__(self):
        return self.max_idxs[-1]

    def __getitem__(self, idx):
        # index into the appropriate dataset to get the trial
        ds_idx = np.argmax(self.max_idxs > idx)
        if ds_idx == 0:
            trial = self.datasets[0][idx]
        else:
            trial = self.datasets[ds_idx][idx - self.max_idxs[ds_idx-1]]

        # combine context cue with actual trial x to get final x
        x = trial.get_x(self.args)
        x_cts = self.x_ctxs[ds_idx]
        x = np.concatenate((x, x_cts))
        # don't need to do that with y
        y = trial.get_y(self.args)
        trial.context = ds_idx
        return x, y, trial

    def get_dset_idx(self, idx):
        return np.argmax(self.max_idxs > idx)

# turns data samples into stuff that can be run through network
def collater(samples):
    xs, ys, trials = list(zip(*samples))
    # pad xs and ys to be the length of the max-length example
    max_len = max([x.shape[-1] for x in xs])
    xs_pad = [np.pad(x, ([0,0],[0,max_len-x.shape[-1]])) for x in xs]
    ys_pad = [np.pad(y, ([0,0],[0,max_len-y.shape[-1]])) for y in ys]
    xs = torch.as_tensor(np.stack(xs_pad), dtype=torch.float)
    ys = torch.as_tensor(np.stack(ys_pad), dtype=torch.float)
    return xs, ys, trials

# creates datasets and dataloaders
def create_loaders(datasets, args, split_test=True, test_size=None, shuffle=True, order_fn=None):
    dsets_train = []
    dsets_test = []
    for d in range(len(datasets)):
        dset = load_rb(datasets[d])
        if not shuffle and order_fn is not None:
            dset = sorted(dset, key=order_fn)
        if split_test:
            cutoff = round(.9 * len(dset))
            dsets_train.append(dset[:cutoff])
            dsets_test.append(dset[cutoff:])
        else:
            dsets_test.append(dset)

    test_set = TrialDataset(dsets_test, args)
    if test_size is None:
        test_size = 128
    if split_test:
        train_set = TrialDataset(dsets_train, args)
        train_loader = DataLoader(train_set, batch_size=args.batch_size, collate_fn=collater, shuffle=shuffle, drop_last=True)
        test_size = min(test_size, len(test_set))
        test_loader = DataLoader(test_set, batch_size=test_size, collate_fn=collater, shuffle=shuffle)
        return (train_set, train_loader), (test_set, test_loader)
    else:
        test_size = min(test_size, len(test_set))
        test_loader = DataLoader(test_set, batch_size=test_size, collate_fn=collater, shuffle=shuffle)
        return (test_set, test_loader)

def get_criteria(args):
    criteria = []
    if 'mse' in args.loss:
        # do this in a roundabout way due to truncated bptt
        fn = nn.MSELoss(reduction='sum')
        def mse(o, t, i, single=False, **kwargs):
            # last dimension is number of timesteps
            # divide by batch size to avoid doing so logging and in test
            # needs all the contexts to be the same length
            loss = 0.
            if single:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                length = i[j].t_len
                loss += fn(t, o) / length / 

            return args.l1 * fn(t, o) / length / args.batch_size
        criteria.append(mse)
    if 'bce' in args.loss:
        weights = args.l3 * torch.ones(1)
        fn = nn.BCEWithLogitsLoss(reduction='sum', pos_weight=weights)
        def bce(o, t, **kwargs):
            return args.l1 * fn(t, o)
        criteria.append(bce)
    if 'mse-e' in args.loss:
        # ONLY FOR RSG AND CSG, WITH [1D] OUTPUT
        # exponential decaying loss from the go time on both sides
        # loss is 1 at go time, 0.5 at set time
        # normalized to the number of timesteps taken
        fn = nn.MSELoss(reduction='none')
        def mse_e(o, t, i, t_ix, single=False):
            loss = 0.
            if single:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                # last dimension is number of timesteps
                t_len = t.shape[-1]
                xr = torch.arange(t_len, dtype=torch.float)
                # placement of go signal in subset of timesteps
                t_g = i[j].rsg[2] - t_ix
                t_p = i[j].t_p
                # exponential loss centred at go time
                # dropping to 0.5 at set time
                lam = -np.log(2) / t_p
                # left half, only use if go time is to the right
                if t_g > 0:
                    xr[:t_g] = torch.exp(-lam * (xr[:t_g] - t_g))
                # right half, can use regardless because python indexing is nice
                xr[t_g:] = torch.exp(lam * (xr[t_g:] - t_g))
                # normalize, just numerically calculate area
                xr = xr / torch.sum(xr) * t_len
                # only the first dimension matters for rsg and csg output
                loss += torch.dot(xr, fn(o[j][0], t[j][0]))
            return args.l2 * loss / args.batch_size
        criteria.append(mse_e)
    if len(criteria) == 0:
        raise NotImplementedError
    return criteria

def get_activation(name):
    if name == 'exp':
        fn = torch.exp
    elif name == 'relu':
        fn = nn.ReLU()
    elif name == 'sigmoid':
        fn = nn.Sigmoid()
    elif name == 'tanh':
        fn = nn.Tanh()
    elif name == 'none':
        fn = lambda x: x
    return fn

def get_output_activation(args):
    return get_activation(args.out_act)

def get_dim(a):
    if hasattr(a, '__iter__'):
        return len(a)
    else:
        return 1

    return l2 * total_loss
