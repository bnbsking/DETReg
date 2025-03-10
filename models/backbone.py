# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Backbone modules.
"""
from collections import OrderedDict

import torch
import torch.nn.functional as F
import torchvision
from torch import nn
from torchvision.models._utils import IntermediateLayerGetter
from typing import Dict, List

from util.misc import NestedTensor, is_main_process

from .position_encoding import build_position_encoding
from .swav_resnet50 import ResNet, resnet50


class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt,
    without which any other models than torchvision.models.resnet[18,34,50,101]
    produce nans.
    """

    def __init__(self, n, eps=1e-5):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))
        self.eps = eps

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = self.eps
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias


class BackboneBase(nn.Module):

    def __init__(self, backbone: nn.Module, train_backbone: bool, return_interm_layers: bool): # resnet50, F, T
        super().__init__()
        for name, parameter in backbone.named_parameters():
            if not train_backbone or 'layer2' not in name and 'layer3' not in name and 'layer4' not in name: # all are True, then freeze the layers
                parameter.requires_grad_(False)
        if return_interm_layers: # True
            # return_layers = {"layer1": "0", "layer2": "1", "layer3": "2", "layer4": "3"}
            return_layers = {"layer2": "0", "layer3": "1", "layer4": "2"}
            self.strides = [8, 16, 32]
            self.num_channels = [512, 1024, 2048]
        else:
            return_layers = {'layer4': "0"}
            self.strides = [32]
            self.num_channels = [2048]
        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers) # output from layer 2,3,4 as named 0,1,2

    def forward(self, tensor_list): # util.misc.NestedTensor(tensor+mask_tensor) or tensors -> dict(str:NestedTensor)
        if isinstance(tensor_list, NestedTensor):
            xs = self.body(tensor_list.tensors)
            out: Dict[str, NestedTensor] = {}
            for name, x in xs.items():
                m = tensor_list.mask
                assert m is not None
                mask = F.interpolate(m[None].float(), size=x.shape[-2:]).to(torch.bool)[0]
                out[name] = NestedTensor(x, mask)
        else:
            out = self.forward_non_nested(tensor_list)
        return out

    def forward_non_nested(self, tensors): # tensors -> dict(str:NestedTensor)
        xs = self.body(tensors)
        out: Dict[str, NestedTensor] = {}
        for name, x in xs.items():
            out[name] = x
        return out

class Backbone(BackboneBase):
    """ResNet backbone with frozen BatchNorm."""

    def __init__(self, name: str,
                 train_backbone: bool,
                 return_interm_layers: bool,
                 dilation: bool,
                 load_backbone: str): # resnet50,F,T,F,swav

        pretrained = load_backbone == 'supervised'
        backbone = getattr(torchvision.models, name)(
            replace_stride_with_dilation=[False, False, dilation],
            pretrained=pretrained, norm_layer=FrozenBatchNorm2d) # resnet50([F,F,F],F,Frozen)
        # resnet 50: (224,224,3) -> ker=(7,7),stride=2 (112,112,64) -> L1:maxPool=(3,3),stride=2 (56,56,256) -> L2:same (28,28,512)
        # -> L3:same (14,14,1024) -> L4:same (7,7,2048) -> GAP and MLP to 1000
        # load the SwAV pre-training model from the url instead of supervised pre-training model
        if name == 'resnet50' and load_backbone == 'swav':
            checkpoint = torch.hub.load_state_dict_from_url('https://dl.fbaipublicfiles.com/deepcluster/swav_800ep_pretrain.pth.tar',map_location="cpu")
            state_dict = {k.replace("module.", ""): v for k, v in checkpoint.items()}
            backbone.load_state_dict(state_dict, strict=False)
        super().__init__(backbone, train_backbone, return_interm_layers) # resnet50, F, T -> resnetModelSwavWeight, False, False
#         if True:
#             inp = torch.rand(1,3,224,224)
#             out = self(inp)
#             for key in out:
#                 print( key, out[key].shape ) # 0,(1,512,28,28) # 1,(1,1024,14,14) # 2,(1,2048,7,7) 
#             raise


class Joiner(nn.Sequential):
    def __init__(self, backbone, position_embedding):
        super().__init__(backbone, position_embedding)
        self.strides = backbone.strides # [8, 16, 32]
        self.num_channels = backbone.num_channels # [512, 1024, 2048]

    def forward(self, tensor_list: NestedTensor):
        # e.g. (1,3,338,600),M(1,338,600) -> [(bz,512,43,75),(bz,1024,22,38),(bz,2048,22,38)], [(bz,256,43,75),(bz,256,22,38),(bz,256,11,19)]
        xs = self[0](tensor_list)
        out: List[NestedTensor] = []
        pos = []
        for name, x in sorted(xs.items()):
            out.append(x)

        # position encoding
        for x in out:
            pos.append(self[1](x).to(x.tensors.dtype))

        return out, pos


def build_backbone(args):
    position_embedding = build_position_encoding(args)
    train_backbone = args.lr_backbone > 0 # False
    return_interm_layers = args.masks or (args.num_feature_levels > 1) # False or 4>1 -> True
    backbone = Backbone(args.backbone, train_backbone, return_interm_layers, args.dilation, load_backbone=args.load_backbone) # resnet50,F,T,F,swav
    model = Joiner(backbone, position_embedding)
    model.num_channels = backbone.num_channels # [512, 1024, 2048]
    return model

def build_swav_backbone(args, device):
    model = resnet50(
        normalize=True,
        hidden_mlp=2048,
        output_dim=128,
    )
    for name, parameter in model.named_parameters():
        parameter.requires_grad_(False)

    checkpoint = torch.hub.load_state_dict_from_url(
        'https://dl.fbaipublicfiles.com/deepcluster/swav_800ep_pretrain.pth.tar', map_location="cpu")
    state_dict = {k.replace("module.", ""): v for k, v in checkpoint.items()}
    missing_keys, unexpected_keys = model.load_state_dict(state_dict, strict=False)
    return model.to(device)

def build_swav_backbone_old(args, device):
    train_backbone = False
    return_interm_layers = args.masks or (args.num_feature_levels > 1) # F or T -> T
    model = Backbone(args.backbone, train_backbone, return_interm_layers, args.dilation, load_backbone=args.load_backbone).to(device)#resnet50,F,T,F,swav
    # (1,3,224,224) -> 0:(1,512,28,28), 1:(1,1024,14,14), 2:(1,2048,7,7)
    def model_func(elem):
        return model(elem)['0'].mean(dim=(2,3)) # (bz,512,28,28) -> (bz,512) GAP
    return model_func

