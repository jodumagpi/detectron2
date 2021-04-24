# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from typing import Dict, List
import fvcore.nn.weight_init as weight_init
import torch
from torch import nn
from torch.nn import functional as F

from detectron2.layers import Conv2d, ConvTranspose2d, ShapeSpec, cat, get_norm
from detectron2.structures import Instances
from detectron2.utils.events import get_event_storage
from detectron2.utils.registry import Registry
from detectron2.structures.masks import PolygonMasks
from itertools import combinations

import numpy as np
import cv2 as cv
import pickle, os
import skimage.io as skio
from shutil import copyfile
from zipfile import ZipFile 

ROI_MASK_HEAD_REGISTRY = Registry("ROI_MASK_HEAD")
ROI_MASK_HEAD_REGISTRY.__doc__ = """
Registry for mask heads, which predicts instance masks given
per-region features.

The registered object will be called with `obj(cfg, input_shape)`.
"""

def mask_rcnn_inference(pred_mask_logits, pred_instances):
    """
    Convert pred_mask_logits to estimated foreground probability masks while also
    extracting only the masks for the predicted classes in pred_instances. For each
    predicted box, the mask of the same class is attached to the instance by adding a
    new "pred_masks" field to pred_instances.

    Args:
        pred_mask_logits (Tensor): A tensor of shape (B, C, Hmask, Wmask) or (B, 1, Hmask, Wmask)
            for class-specific or class-agnostic, where B is the total number of predicted masks
            in all images, C is the number of foreground classes, and Hmask, Wmask are the height
            and width of the mask predictions. The values are logits.
        pred_instances (list[Instances]): A list of N Instances, where N is the number of images
            in the batch. Each Instances must have field "pred_classes".

    Returns:
        None. pred_instances will contain an extra "pred_masks" field storing a mask of size (Hmask,
            Wmask) for predicted class. Note that the masks are returned as a soft (non-quantized)
            masks the resolution predicted by the network; post-processing steps, such as resizing
            the predicted masks to the original image resolution and/or binarizing them, is left
            to the caller.
    """
    cls_agnostic_mask = pred_mask_logits.size(1) == 1

    if cls_agnostic_mask:
        mask_probs_pred = pred_mask_logits.sigmoid()
    else:
        # Select masks corresponding to the predicted classes
        num_masks = pred_mask_logits.shape[0]
        class_pred = cat([i.pred_classes for i in pred_instances])
        indices = torch.arange(num_masks, device=class_pred.device)
        mask_probs_pred = pred_mask_logits[indices, class_pred][:, None].sigmoid()
    # mask_probs_pred.shape: (B, 1, Hmask, Wmask)

    num_boxes_per_image = [len(i) for i in pred_instances]
    mask_probs_pred = mask_probs_pred.split(num_boxes_per_image, dim=0)

    for prob, instances in zip(mask_probs_pred, pred_instances):
        instances.pred_masks = prob  # (1, Hmask, Wmask)


class BaseMaskRCNNHead(nn.Module):
    """
    Implement the basic Mask R-CNN losses and inference logic.
    """

    def __init__(self, cfg, input_shape):
        super().__init__()
        self.vis_period = cfg.VIS_PERIOD
        # log vars to learn the weights
        self.log_vars = nn.Parameter(torch.zeros(3))

    def mask_rcnn_loss(self, pred_mask_logits, instances, vis_period=0):
        """
        Compute the mask prediction loss defined in the Mask R-CNN paper.
        Args:
            pred_mask_logits (Tensor): A tensor of shape (B, C, Hmask, Wmask) or (B, 1, Hmask, Wmask)
                for class-specific or class-agnostic, where B is the total number of predicted masks
                in all images, C is the number of foreground classes, and Hmask, Wmask are the height
                and width of the mask predictions. The values are logits.
            instances (list[Instances]): A list of N Instances, where N is the number of images
                in the batch. These instances are in 1:1
                correspondence with the pred_mask_logits. The ground-truth labels (class, box, mask,
                ...) associated with each instance are stored in fields.
            vis_period (int): the period (in steps) to dump visualization.
        Returns:
            mask_loss (Tensor): A scalar tensor containing the loss.
        """

        # pred_mask_logits initially contains all the mask prediction for each class

        # -------------------------------------------------------------------------------------------- #

        cls_agnostic_mask = pred_mask_logits.size(1) == 1
        total_num_masks = pred_mask_logits.size(0)
        mask_side_len = pred_mask_logits.size(2)
        assert pred_mask_logits.size(2) == pred_mask_logits.size(3), "Mask prediction must be square!"

        gt_classes = []
        gt_masks = []

        # -------------------------------------------------------------------------------------------- #

        # containers of the weights per instance
        boundary_penalty = [] 
        roi_penalty = []
        overlap_penalty = []
        # hyperparameter
        BNDRY_WT = 0
        # save model wts and print info
        FILENAME = 'FILENAME'
        file_paths = get_all_file_paths('./output/')
        storage = get_event_storage()
        if storage.iter % 50 == 0:
            print('Learnable weights: {}'.format(self.log_vars.detach().cpu().numpy()))
        if storage.iter > 0 and storage.iter % 6000 == 0:
            with ZipFile('{}.zip'.format(FILENAME),'w') as zip: 
                for file in file_paths: 
                    zip.write(file) 
            copyfile('./{}.zip'.format(FILENAME), './drive/My Drive/{}.zip'.format(FILENAME))
            print('Saving weights...')

        for instances_per_image in instances:

            # ---------------------------------------------------------------------------------------- #

            if len(instances_per_image) == 0:
                continue
            if not cls_agnostic_mask:
                gt_classes_per_image = instances_per_image.gt_classes.to(dtype=torch.int64)
                gt_classes.append(gt_classes_per_image)

            gt_masks_per_image = instances_per_image.gt_masks.crop_and_resize(
                instances_per_image.proposal_boxes.tensor, mask_side_len
            ).to(device=pred_mask_logits.device)
            # A tensor of shape (N, M, M), N=#instances in the image; M=mask_side_len
            gt_masks.append(gt_masks_per_image)

            # ---------------------------------------------------------------------------------------- #

            # get the boundary pixels
            for m in gt_masks_per_image.detach().cpu().numpy(): # for each ground truth mask
                kernel = np.ones((3,3), np.uint8) # small square kernel for dilation and erosion
                background = np.zeros((mask_side_len, mask_side_len)) # container of the contour
                cnts, _ = cv.findContours(np.where(m==True,255,0).astype(np.uint8), cv.RETR_EXTERNAL,
                                                                    cv.CHAIN_APPROX_SIMPLE)
                cv.drawContours(background, cnts, -1, 1, -1) # draw the contours to the container
                dilation = cv.dilate(background, kernel).astype(np.uint8) # dilate the contours
                erosion = cv.erode(background, kernel).astype(np.uint8) # erode the contours
                bound_pixels = np.bitwise_xor(dilation, erosion) # get the boundary bixels
                # aggregate the boundary pixels
                boundary_penalty.append(torch.from_numpy(bound_pixels))
                # for checks
                # skio.imsave("dilated.png", dilation)
                # skio.imsave("eroded.png", erosion)
                # skio.imsave("boundary.png", bound_pixels)

            # solve for roi penalty
            # get the gt bboxes for each prediciton
            ins_gt_boxes = np.asarray([x.detach().cpu().numpy() for x in instances_per_image.gt_boxes])
            # get the real bboxes (unique) and the number of times they were predicted
            gt_boxes, roi_counts = np.unique(ins_gt_boxes, axis=0, return_counts=True)
            # find the ROIs that are the closest to the ground truth labels
            # place holder for the roi penalty for each image
            img_roi_penalty = torch.ones((len(instances_per_image), 1, 1))
            for i, gt_box in enumerate(gt_boxes):
                # solve for the current closest iou and the index of the current best bbox
                best_iou, best_box = 0, 0
                # loop over the predicted bboxes
                for j, box in enumerate(instances_per_image.proposal_boxes):
                    iou = bb_intersection_over_union(gt_box, box.detach().cpu().numpy())
                    if best_iou < iou:
                        best_iou = iou # replace current best iou
                        best_box = j # save the index of the current best bbox
                # after searching, place the penalty on the index of the closest bbox
                img_roi_penalty[best_box] = torch.tensor(roi_counts[i].item())
            # aggregate the roi penalties
            roi_penalty.append(img_roi_penalty)

            # get the overlapping pixels
            # placeholder for the volume of gt masks for each gt bbox
            img_masks = []
            done = [] # placeholder for unique gt mask
            for x in instances_per_image.gt_masks: 
                if list(x[0]) not in done:
                    done.append(list(x[0]))
                    # generate the current gt mask for the instance for all predictions
                    temp_msk = [[x[0]] for i in range(len(instances_per_image))]
                    img_masks.append(PolygonMasks(temp_msk).crop_and_resize(
                            instances_per_image.proposal_boxes.tensor, mask_side_len
                            ).to(dtype=torch.float32, device="cuda"))
            # there is possibly an overlap if there are more than 1 instance
            if len(img_masks) > 1:
                combs = combinations(img_masks, 2) # pair up for each bbox??
                temp = []
                for c in combs:
                    temp.append(c[0]*c[1])
                per_ins_overlap_masks = sum(temp)
                per_ins_overlap_masks *= gt_masks_per_image.to(dtype=torch.float32)
            else: # no overlap!
                per_ins_overlap_masks = torch.zeros(gt_masks_per_image.shape)
            # aggregate the overlap penalties
            overlap_penalty.append(per_ins_overlap_masks.to(device="cuda"))
            
        # aggregate boundary pixels for each instance to create a volume of boundary masks
        # convert the boundary to mask to penalty
        boundary_penalty = torch.where(torch.stack(boundary_penalty)==1, torch.ones(1)*BNDRY_WT, 
                                                                    torch.ones(1)).to(device="cuda")

        #np.save("boundary.npy", boundary_penalty.detach().cpu().numpy())

        # aggregate the roi penalties from each image
        roi_penalty = torch.cat(roi_penalty, 0).to(device="cuda")
        #np.save("roi.npy", roi_penalty.detach().cpu().numpy())

        # aggregate the overlap penalties from each image
        # get the real number of overlapping objects
        overlap_penalty = quad(torch.cat(overlap_penalty, 0)).to(dtype=torch.float32, device="cuda")

        #np.save("overlap.npy", overlap_penalty.detach().cpu().numpy())

        # -------------------------------------------------------------------------------------------- #

        if len(gt_masks) == 0:
            return pred_mask_logits.sum() * 0

        gt_masks = cat(gt_masks, dim=0)

        if cls_agnostic_mask:
            pred_mask_logits = pred_mask_logits[:, 0]
        else:
            indices = torch.arange(total_num_masks)
            gt_classes = cat(gt_classes, dim=0)
            pred_mask_logits = pred_mask_logits[indices, gt_classes]

        if gt_masks.dtype == torch.bool:
            gt_masks_bool = gt_masks
        else:
            # Here we allow gt_masks to be float as well (depend on the implementation of rasterize())
            gt_masks_bool = gt_masks > 0.5
        gt_masks = gt_masks.to(dtype=torch.float32)

        # Log the training accuracy (using gt classes and 0.5 threshold)
        mask_incorrect = (pred_mask_logits > 0.0) != gt_masks_bool
        mask_accuracy = 1 - (mask_incorrect.sum().item() / max(mask_incorrect.numel(), 1.0))
        num_positive = gt_masks_bool.sum().item()
        false_positive = (mask_incorrect & ~gt_masks_bool).sum().item() / max(
            gt_masks_bool.numel() - num_positive, 1.0
        )
        false_negative = (mask_incorrect & gt_masks_bool).sum().item() / max(num_positive, 1.0)

        storage = get_event_storage()
        storage.put_scalar("mask_rcnn/accuracy", mask_accuracy)
        storage.put_scalar("mask_rcnn/false_positive", false_positive)
        storage.put_scalar("mask_rcnn/false_negative", false_negative)
        if vis_period > 0 and storage.iter % vis_period == 0:
            pred_masks = pred_mask_logits.sigmoid()
            vis_masks = torch.cat([pred_masks, gt_masks], axis=2)
            name = "Left: mask prediction;   Right: mask GT"
            for idx, vis_mask in enumerate(vis_masks):
                vis_mask = torch.stack([vis_mask] * 3, axis=0)
                storage.put_image(name + f" ({idx})", vis_mask)

        # -------------------------------------------------------------------------------------------- #

        # pred_mask_logits will then be left to only include the mask for the predicted instance

        # apply penalties to the losses
        boundary_mask_loss = F.binary_cross_entropy_with_logits(pred_mask_logits, 
                                gt_masks, weight=boundary_penalty, reduction="none")

        roi_mask_loss = F.binary_cross_entropy_with_logits(pred_mask_logits, 
                                gt_masks, weight=roi_penalty, reduction="none")

        overlap_mask_loss = F.binary_cross_entropy_with_logits(pred_mask_logits, 
                                gt_masks, weight=overlap_penalty, reduction="none")

        # calcualte relative weighing of the losses
        precision1 = torch.exp(-self.log_vars[0])
        weighted_mask_loss = precision1 * boundary_mask_loss + self.log_vars[0]

        precision2 = torch.exp(-self.log_vars[1])
        weighted_mask_loss += precision2 * roi_mask_loss + self.log_vars[1]

        precision3 = torch.exp(-self.log_vars[2])
        weighted_mask_loss += precision3 * overlap_mask_loss + self.log_vars[2]
        
        return torch.mean(weighted_mask_loss)

    def forward(self, x: Dict[str, torch.Tensor], instances: List[Instances]):
        """
        Args:
            x (dict[str,Tensor]): input region feature(s) provided by :class:`ROIHeads`.
            instances (list[Instances]): contains the boxes & labels corresponding
                to the input features.
                Exact format is up to its caller to decide.
                Typically, this is the foreground instances in training, with
                "proposal_boxes" field and other gt annotations.
                In inference, it contains boxes that are already predicted.

        Returns:
            A dict of losses in training. The predicted "instances" in inference.
        """
        x = self.layers(x)
        if self.training:
            return {"loss_mask": self.mask_rcnn_loss(x, instances, self.vis_period)}
        else:
            mask_rcnn_inference(x, instances)
            return instances

    def layers(self, x):
        """
        Neural network layers that makes predictions from input features.
        """
        raise NotImplementedError


@ROI_MASK_HEAD_REGISTRY.register()
class MaskRCNNConvUpsampleHead(BaseMaskRCNNHead):
    """
    A mask head with several conv layers, plus an upsample layer (with `ConvTranspose2d`).
    """

    def __init__(self, cfg, input_shape: ShapeSpec):
        """
        The following attributes are parsed from config:
            num_conv: the number of conv layers
            conv_dim: the dimension of the conv layers
            norm: normalization for the conv layers
        """
        super().__init__(cfg, input_shape)

        # fmt: off
        num_classes       = cfg.MODEL.ROI_HEADS.NUM_CLASSES
        conv_dims         = cfg.MODEL.ROI_MASK_HEAD.CONV_DIM
        self.norm         = cfg.MODEL.ROI_MASK_HEAD.NORM
        num_conv          = cfg.MODEL.ROI_MASK_HEAD.NUM_CONV
        input_channels    = input_shape.channels
        cls_agnostic_mask = cfg.MODEL.ROI_MASK_HEAD.CLS_AGNOSTIC_MASK
        # fmt: on

        self.conv_norm_relus = []

        for k in range(num_conv):
            conv = Conv2d(
                input_channels if k == 0 else conv_dims,
                conv_dims,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=not self.norm,
                norm=get_norm(self.norm, conv_dims),
                activation=F.relu,
            )
            self.add_module("mask_fcn{}".format(k + 1), conv)
            self.conv_norm_relus.append(conv)

        self.deconv = ConvTranspose2d(
            conv_dims if num_conv > 0 else input_channels,
            conv_dims,
            kernel_size=2,
            stride=2,
            padding=0,
        )

        num_mask_classes = 1 if cls_agnostic_mask else num_classes
        self.predictor = Conv2d(conv_dims, num_mask_classes, kernel_size=1, stride=1, padding=0)

        for layer in self.conv_norm_relus + [self.deconv]:
            weight_init.c2_msra_fill(layer)
        # use normal distribution initialization for mask prediction layer
        nn.init.normal_(self.predictor.weight, std=0.001)
        if self.predictor.bias is not None:
            nn.init.constant_(self.predictor.bias, 0)

    def layers(self, x):
        for layer in self.conv_norm_relus:
            x = layer(x)
        x = F.relu(self.deconv(x))
        return self.predictor(x)

def bb_intersection_over_union(boxA, boxB):
    # determine the (x, y)-coordinates of the intersection rectangle
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    # compute the area of intersection rectangle
    interArea = abs(max((xB - xA, 0)) * max((yB - yA), 0))
    if interArea == 0:
        return 0
    # compute the area of both the prediction and ground-truth
    # rectangles
    boxAArea = abs((boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    boxBArea = abs((boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))

    # compute the intersection over union by taking the intersection
    # area and dividing it by the sum of prediction + ground-truth
    # areas - the interesection area
    iou = interArea / float(boxAArea + boxBArea - interArea)

    # return the intersection over union value
    return iou

def build_mask_head(cfg, input_shape):
    """
    Build a mask head defined by `cfg.MODEL.ROI_MASK_HEAD.NAME`.
    """
    name = cfg.MODEL.ROI_MASK_HEAD.NAME
    return ROI_MASK_HEAD_REGISTRY.get(name)(cfg, input_shape)


def quad(c):
    c = -2 * c
    x1 = (1 + torch.sqrt(1-4*c)) / 2
    x2 = (1 - torch.sqrt(1-4*c)) / 2

    return torch.max(x1, x2)

def get_all_file_paths(directory): 
  
    # initializing empty file paths list 
    file_paths = [] 
  
    # crawling through directory and subdirectories 
    for root, directories, files in os.walk(directory): 
        for filename in files:
            filepath = os.path.join(root, filename) 
            file_paths.append(filepath) 
    # returning all file paths 
    return file_paths
