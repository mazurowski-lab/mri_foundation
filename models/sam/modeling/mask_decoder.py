# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import torch
from torch import nn
from torch.nn import functional as F

from typing import List, Tuple, Type

from .common import LayerNorm2d, Adapter
from .vit import TransformerEncoder
from einops import rearrange


class SmallDecoder(nn.Module):
    def __init__(
        self,
        input_chans = 256,
        prompt_embed_dim=256,
        img_size = (256,256),
        patch_size = 1,
        scale = 256 ** -0.5,
        activation = nn.GELU,
        depth = 1,
        n_cls=1)-> None:
        super().__init__()
        self.scale = scale
        self.n_cls = n_cls
        self.img_size = img_size
        self.patch_size = patch_size
        
        self.cls_emb = nn.Parameter(
            torch.randn([1, n_cls, prompt_embed_dim]))
        self.dec_proj = nn.Linear(prompt_embed_dim, prompt_embed_dim)
        
        self.decoder_norm = nn.LayerNorm(prompt_embed_dim)
        self.mask_norm = nn.LayerNorm(n_cls)
        
        self.proj_patch = nn.Parameter(
            self.scale * torch.randn(prompt_embed_dim, prompt_embed_dim))
        self.proj_classes = nn.Parameter(
            self.scale * torch.randn(prompt_embed_dim, prompt_embed_dim))
        
        self.blocks = TransformerEncoder(depth=depth)
        
        self.upsampling = nn.Sequential(
            nn.ConvTranspose2d(prompt_embed_dim, prompt_embed_dim, kernel_size=2, stride=2),
            LayerNorm2d(prompt_embed_dim),
            activation(),
            nn.ConvTranspose2d(prompt_embed_dim, prompt_embed_dim, kernel_size=2, stride=2),
            activation(),
        )
        
    def forward(self,
        image_embedding: torch.Tensor)-> torch.Tensor:
        b, c, h, w = image_embedding.shape
        #x = self.input(image_embedding) # b*in_chan*h*w -> b*1*h*w
        image_embedding = image_embedding.flatten(2).permute(0, 2, 1)
        #print('in shape:',image_embedding.shape) # b*(h*w)*emb_dim
    
        H, W = self.img_size
        GS = H//self.patch_size
        x = self.dec_proj(image_embedding)
        
        # Adding a cls token for each segmenting class
        cls_emb = self.cls_emb.expand(x.size(0), -1, -1)
        x = torch.cat((x, cls_emb), 1)
        #print('x shape:',x.shape)
        out = (self.blocks(x)) # one transformer block
        #print('out shape:',out.shape)
        
        x = self.decoder_norm(out)

        patches, cls_seg_feat = x[:, :-self.n_cls], x[:, -self.n_cls:]
        
        # new added up-sampling        
        patches = patches.transpose(1,2).view(b, c, h, w) # b * 256 * 64* 64
        patches = self.upsampling(patches) # b * 256 * 256 * 256
        patches = patches.flatten(2).permute(0, 2, 1) # b * 256^2 * 256
        
        
        patches = patches @ self.proj_patch # b * 256^2 * 256
        cls_seg_feat = cls_seg_feat @ self.proj_classes # b * 1 * 256
        
        
        patches = patches / patches.norm(dim=-1, keepdim=True) # b * 256^2 * 256
        #print('patches shape:',patches.shape)
        cls_seg_feat = cls_seg_feat / cls_seg_feat.norm(dim=-1, keepdim=True)  # b * 1 * 256
        #print('cls_seg shape:',cls_seg_feat.shape)
        masks = patches @ cls_seg_feat.transpose(1, 2) 
        
        #masks = self.mask_norm(masks)
        
        #print('after norm:',masks)
        masks = rearrange(masks, "b (h w) n -> b n h w", h=int(GS))
        #print(masks)
        #out = F.interpolate(masks, size=(H, W), mode="bilinear")
        out = masks
        return out
        


class MaskDecoder(nn.Module):
    def __init__(
        self,
        *,
        transformer_dim: int,
        transformer: nn.Module,
        num_multimask_outputs: int = 3,
        activation: Type[nn.Module] = nn.GELU,
        iou_head_depth: int = 3,
        iou_head_hidden_dim: int = 256,
        extra_layer = False
    ) -> None:
        """
        Predicts masks given an image and prompt embeddings, using a
        transformer architecture.

        Arguments:
          transformer_dim (int): the channel dimension of the transformer
          transformer (nn.Module): the transformer used to predict masks
          num_multimask_outputs (int): the number of masks to predict
            when disambiguating masks
          activation (nn.Module): the type of activation to use when
            upscaling masks
          iou_head_depth (int): the depth of the MLP used to predict
            mask quality
          iou_head_hidden_dim (int): the hidden dimension of the MLP
            used to predict mask quality
        """
        super().__init__()
        self.transformer_dim = transformer_dim
        self.transformer = transformer

        self.num_multimask_outputs = num_multimask_outputs

        self.iou_token = nn.Embedding(1, transformer_dim)
        self.num_mask_tokens = num_multimask_outputs + 1
        self.mask_tokens = nn.Embedding(self.num_mask_tokens, transformer_dim)
        
        if not extra_layer:
            self.output_upscaling = nn.Sequential(
                nn.ConvTranspose2d(transformer_dim, transformer_dim // 4, kernel_size=2, stride=2),
                LayerNorm2d(transformer_dim // 4),
                activation(),
                nn.ConvTranspose2d(transformer_dim // 4, transformer_dim // 8, kernel_size=2, stride=2),
                activation(),
            )
            self.output_hypernetworks_mlps = nn.ModuleList(
                [
                    MLP(transformer_dim, transformer_dim, transformer_dim // 8, 3)
                    for i in range(self.num_mask_tokens)
                ]
            )
        else:
            self.output_upscaling = nn.Sequential(
                nn.ConvTranspose2d(transformer_dim, transformer_dim // 4, kernel_size=2, stride=2),
                LayerNorm2d(transformer_dim // 4),
                activation(),
                nn.ConvTranspose2d(transformer_dim // 4, transformer_dim // 8, kernel_size=2, stride=2),
                LayerNorm2d(transformer_dim // 8),
                activation(),
                nn.ConvTranspose2d(transformer_dim // 8, transformer_dim // 16, kernel_size=2, stride=2),
                LayerNorm2d(transformer_dim // 16),
                activation(),
                nn.ConvTranspose2d(transformer_dim // 16, transformer_dim // 32, kernel_size=2, stride=2),
                activation(),
            )
            self.output_hypernetworks_mlps = nn.ModuleList(
                [
                    MLP(transformer_dim, transformer_dim, transformer_dim // 32, 3)
                    for i in range(self.num_mask_tokens)
                ]
            )

        self.iou_prediction_head = MLP(
            transformer_dim, iou_head_hidden_dim, self.num_mask_tokens, iou_head_depth
        )

    def forward(
        self,
        image_embeddings: torch.Tensor,
        image_pe: torch.Tensor,
        sparse_prompt_embeddings: torch.Tensor,
        dense_prompt_embeddings: torch.Tensor,
        multimask_output: bool,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Predict masks given image and prompt embeddings.

        Arguments:
          image_embeddings (torch.Tensor): the embeddings from the image encoder
          image_pe (torch.Tensor): positional encoding with the shape of image_embeddings
          sparse_prompt_embeddings (torch.Tensor): the embeddings of the points and boxes
          dense_prompt_embeddings (torch.Tensor): the embeddings of the mask inputs
          multimask_output (bool): Whether to return multiple masks or a single
            mask.

        Returns:
          torch.Tensor: batched predicted masks
          torch.Tensor: batched predictions of mask quality
        """
        masks, iou_pred = self.predict_masks(
            image_embeddings=image_embeddings,
            image_pe=image_pe,
            sparse_prompt_embeddings=sparse_prompt_embeddings,
            dense_prompt_embeddings=dense_prompt_embeddings,
        )

        # Select the correct mask or masks for output
        if multimask_output:
            mask_slice = slice(1, None)
        else:
            mask_slice = slice(0, 1)
        masks = masks[:, mask_slice, :, :]
        iou_pred = iou_pred[:, mask_slice]

        # Prepare output
        return masks, iou_pred

    def predict_masks(
        self,
        image_embeddings: torch.Tensor,
        image_pe: torch.Tensor,
        sparse_prompt_embeddings: torch.Tensor,
        dense_prompt_embeddings: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Predicts masks. See 'forward' for more details."""
        # Concatenate output tokens
        output_tokens = torch.cat([self.iou_token.weight, self.mask_tokens.weight], dim=0)
        output_tokens = output_tokens.unsqueeze(0).expand(sparse_prompt_embeddings.size(0), -1, -1)
        tokens = torch.cat((output_tokens, sparse_prompt_embeddings), dim=1)

        # Expand per-image data in batch direction to be per-mask
        if image_embeddings.shape[0] != tokens.shape[0]:
            src = torch.repeat_interleave(image_embeddings, tokens.shape[0], dim=0)
        else:
            src = image_embeddings
        src = src + dense_prompt_embeddings
        pos_src = torch.repeat_interleave(image_pe, tokens.shape[0], dim=0)
        b, c, h, w = src.shape

        # Run the transformer
        hs, src = self.transformer(src, pos_src, tokens)
        iou_token_out = hs[:, 0, :]
        mask_tokens_out = hs[:, 1 : (1 + self.num_mask_tokens), :]

        # Upscale mask embeddings and predict masks using the mask tokens
        src = src.transpose(1, 2).view(b, c, h, w)
        upscaled_embedding = self.output_upscaling(src)
        hyper_in_list: List[torch.Tensor] = []
        for i in range(self.num_mask_tokens):
            hyper_in_list.append(self.output_hypernetworks_mlps[i](mask_tokens_out[:, i, :]))
        hyper_in = torch.stack(hyper_in_list, dim=1)
        b, c, h, w = upscaled_embedding.shape
        masks = (hyper_in @ upscaled_embedding.view(b, c, h * w)).view(b, -1, h, w)

        # Generate mask quality predictions
        iou_pred = self.iou_prediction_head(iou_token_out)

        return masks, iou_pred


# Lightly adapted from
# https://github.com/facebookresearch/MaskFormer/blob/main/mask_former/modeling/transformer/transformer_predictor.py # noqa
class MLP(nn.Module):
    def __init__(
        self,
        input_dim: int,
        hidden_dim: int,
        output_dim: int,
        num_layers: int,
        sigmoid_output: bool = False,
    ) -> None:
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(
            nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim])
        )
        self.sigmoid_output = sigmoid_output

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        if self.sigmoid_output:
            x = F.sigmoid(x)
        return x
