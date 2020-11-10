
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

import pdb

import random

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

def get_criteria(args):
    criteria = []
    if 'mse' in args.losses:
        fn = nn.MSELoss(reduction='sum')
        def mse(o, t, i=None):
            return args.l1 * fn(t, o)
        criteria.append(mse)
    if 'bce' in args.losses:
        weights = args.l3 * torch.ones(1)
        fn = nn.BCEWithLogitsLoss(reduction='sum', pos_weight=weights)
        def bce(o, t, i=None):
            return args.l1 * fn(t, o)
        criteria.append(bce)
    if 'mse-w' in args.losses:
        fn = nn.MSELoss(reduction='sum')
        def mse_w(o, t, i):
            loss = 0.
            if len(o.shape) == 1:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                t_set, t_go = i[j][1], i[j][2]
                t_p = t_go - t_set
                # using interval from t_set to t_go + t_p
                loss += t.shape[1] / t_p * fn(o[j,t_set:t_go+t_p+1], t[j,t_set:t_go+t_p+1])
            return args.l2 * loss
        criteria.append(mse_w)
    if 'mse-w2' in args.losses:
        # use this and only this. no loss defined after we reach the goal
        # l1 for normal loss pre-set, l2 for loss post-set
        fn = nn.MSELoss(reduction='sum')
        def mse_w(o, t, i):
            loss = 0.
            if len(o.shape) == 1:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                t_set, t_go = i[j][1], i[j][2]
                t_p = t_go - t_set
                # using interval from t_set to t_go + t_p, for the windowed loss
                loss += args.l2 * t.shape[1] / (2 * t_p) * fn(o[j,t_set:t_go+t_p], t[j,t_set:t_go+t_p])
                # normal nonwindowed loss for times before t_set
                loss += args.l1 * t.shape[1] / t_set * fn(o[j,:t_set], t[j,:t_set])
            return loss
        criteria.append(mse_w)
    if 'bce-w' in args.losses:
        weights = args.l4 * torch.ones(1)
        fn = nn.BCEWithLogitsLoss(reduction='sum', pos_weight=weights)
        def bce_w(o, t, i):
            loss = 0.
            if len(o.shape) == 1:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                t_set, t_go = i[j][1], i[j][2]
                t_p = t_go - t_set
                # using interval from t_set to t_go + t_p
                # normalizing by length of the whole trial over length of penalized window
                loss += t.shape[1] / t_p * fn(o[j,t_set:t_set+t_p+1], t[j,t_set:t_go+t_p+1])
            return args.l2 * loss
        criteria.append(bce_w)
    if 'mse-g' in args.losses:
        fn = nn.MSELoss(reduction='sum')
        def mse_g(o, t, i):
            loss = 0.
            if len(o.shape) == 1:
                o = o.unsqueeze(0)
                t = t.unsqueeze(0)
                i = [i]
            for j in range(len(t)):
                t_ready, t_go = i[j][0], i[j][2]
                first_t = torch.nonzero(o[j][t_ready:] > 1)
                if len(first_t) == 0:
                    first_t = torch.tensor(len(o[j])) - 1
                else:
                    first_t = first_t[0,0] + t_ready
                t_new = None
                if t_go > first_t:
                    o_new = o[j][first_t:t_go+1]
                    t_new = t[j][first_t:t_go+1]
                    # w = torch.arange(t_go+1-first_t,0,-1)
                elif t_go < first_t:
                    o_new = o[j][t_go:first_t + 1]
                    t_new = t[j][t_go:first_t + 1]
                    # w = torch.arange(0,first_t+1-t_go,1)
                if t_new is not None:
                    g_loss = fn(o_new, t_new)
                    # g_loss = torch.sum(w * torch.square(t_new - o_new))
                    loss += g_loss
            return args.l2 * loss
        criteria.append(mse_g)
    if len(criteria) == 0:
        raise NotImplementedError
    return criteria

def get_output_activation(args):
    if args.out_act == 'exp':
        fn = torch.exp
    elif args.out_act == 'relu':
        fn = nn.ReLU()
    elif args.out_act == 'none':
        fn = lambda x: x
    return fn

# given batch, get the x, y pairs and turn them into Tensors
def get_x_y_info(args, batch):
    x, y, info = list(zip(*batch))
    x = torch.as_tensor(x, dtype=torch.float)
    y = torch.as_tensor(y, dtype=torch.float)
    if args.same_signal:
        x = torch.sum(x, dim=-1)
    return x, y, info

def get_dim(a):
    if hasattr(a, '__iter__'):
        return len(a)
    else:
        return 1

def corrupt_ix(args, x):
    if args.x_noise == 0:
        return x
    pulses = torch.nonzero(x)[:,1].reshape(x.shape[0], args.L, -1).repeat(1,1,10).float()
    pulses += torch.randn_like(pulses) * args.x_noise
    pulses = torch.round(pulses).long()
    x = torch.zeros_like(x)
    for i in range(x.shape[0]):
        for j in range(args.L):
            nums, counts = torch.unique(pulses[i,j], return_counts=True)
            x[i,nums,j] = counts / 10
    if args.L == 1:
        x = x.squeeze(2)
    return x

def shift_ix(args, x, info):
    if args.m_noise == 0:
        return x
    for i in range(x.shape[0]):
        if args.L == 1:
            t_p = info[i][1] - info[i][0]
            disp = np.rint(np.random.normal(0, args.m_noise*t_p/50))
            raise NotImplementedError
        else:
            t_p = info[i][1] - info[i][0]
            disp = int(np.random.normal(0, args.m_noise*t_p/50))
            x[i,:,0] = x[i,:,0].roll(disp)
    return x


def mse2_loss(x, outs, info, l1, l2, extras=False):
    total_loss = 0.
    first_ts = []
    if len(outs.shape) == 1:
        x = x.unsqueeze(0)
        outs = outs.unsqueeze(0)
        info = [info]
    for j in range(len(x)):
        # pdb.set_trace()
        ready, go = info[j][0], info[j][2]
        # getting the index of the first timestep where output is above threshold
        first_t = torch.nonzero(outs[j][ready:] > l1)
        if len(first_t) == 0:
            first_t = torch.tensor(len(x[j])) - 1
        else:
            first_t = first_t[0,0] + ready
        targets = None
        # losses defined on interval b/w go and first_t
        if go > first_t:
            relevant_outs = outs[j][first_t:go+1]
            targets = torch.zeros_like(relevant_outs)
            weights = torch.arange(go+1-first_t,0,-1)
        elif go < first_t:
            relevant_outs = outs[j][go:first_t + 1]
            targets = l1 * torch.ones_like(relevant_outs)
            weights = torch.arange(0,first_t+1-go,1)
        first_ts.append(first_t)
        if targets is not None:
            mse2_loss = torch.sum(weights * torch.square(targets - relevant_outs))
            # mse2_loss = diff * nn.MSELoss(reduction='sum')(targets, relevant_outs)
            total_loss += mse2_loss
    if extras:
        first_t_avg = sum(first_ts) / len(first_ts)
        return l2 * total_loss, first_t_avg
    return l2 * total_loss