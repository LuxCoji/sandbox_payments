"""
TREASURE model  (paper Section 3.2-3.4).

  InputModule  : num -> log -> linear ; cat -> embeddings ; concat -> MLP -> H     (Fig 4)
  TREASURE     : [H_static, H_dyn_0..t] -> causal Transformer decoder (no pos-enc)  (Fig 3)
  OutputModule : two identical sub-modules -> next-txn attrs & current signals      (Fig 5)

Numerical outputs are a log-normal distribution (mu, log-sigma). Categorical outputs
expose H_a (attribute-specific projection) and an output embedding table E, so the loss
module can use full cross-entropy (low-card) or InfoNCE over shared negatives (high-card).
"""
import math
import torch
import torch.nn as nn

from .config import Schema, ModelConfig


def signed_log(x):
    """log1p on magnitude, sign-preserving. All our numericals are >=0 but this is safe."""
    return torch.sign(x) * torch.log1p(torch.abs(x))


class InputModule(nn.Module):
    """Encodes ONE attribute vector (static, or one dynamic transaction) into R^{d_model}."""
    def __init__(self, num_attrs, cat_attrs, cfg: ModelConfig):
        super().__init__()
        self.n_num = len(num_attrs)
        self.cat_attrs = cat_attrs
        e = cfg.cat_emb_dim
        self.embeddings = nn.ModuleList([nn.Embedding(a.cardinality, e) for a in cat_attrs])
        if self.n_num > 0:
            self.num_proj = nn.Linear(self.n_num, e)          # numericals -> one "field" of width e
        fused_in = e * (len(cat_attrs) + (1 if self.n_num else 0))
        self.in_proj = nn.Linear(fused_in, cfg.d_model)
        blocks = []
        for _ in range(cfg.input_layers):
            blocks += [nn.Linear(cfg.d_model, cfg.d_model), nn.GELU()]
        self.mlp = nn.Sequential(*blocks)
        self.norm = nn.LayerNorm(cfg.d_model)

    def forward(self, x_num, x_cat):
        # x_num: [*, n_num]   x_cat: [*, n_cat]
        parts = []
        if self.n_num > 0:
            parts.append(self.num_proj(signed_log(x_num)))
        for i, emb in enumerate(self.embeddings):
            parts.append(emb(x_cat[..., i]))
        h = torch.cat(parts, dim=-1)
        h = self.in_proj(h)
        h = self.mlp(h)
        return self.norm(h)


class DecoderBlock(nn.Module):
    """Pre-norm Transformer decoder block with causal masked self-attention (Fig 3 inset)."""
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.ln1 = nn.LayerNorm(cfg.d_model)
        self.attn = nn.MultiheadAttention(cfg.d_model, cfg.n_heads,
                                          dropout=cfg.dropout, batch_first=True)
        self.ln2 = nn.LayerNorm(cfg.d_model)
        self.ff = nn.Sequential(
            nn.Linear(cfg.d_model, 4 * cfg.d_model), nn.ReLU(),
            nn.Linear(4 * cfg.d_model, cfg.d_model),
        )
        self.drop = nn.Dropout(cfg.dropout)

    def forward(self, x, attn_mask, key_padding_mask):
        h = self.ln1(x)
        a, _ = self.attn(h, h, h, attn_mask=attn_mask,
                         key_padding_mask=key_padding_mask, need_weights=False)
        x = x + self.drop(a)
        x = x + self.drop(self.ff(self.ln2(x)))
        return x


class OutputSubModule(nn.Module):
    """Predicts numerical (mu, log-sigma) + categorical (H_a, E) for a set of attributes."""
    def __init__(self, num_attrs, cat_attrs, cfg: ModelConfig, out_emb_dim: int = 64):
        super().__init__()
        self.n_num = len(num_attrs)
        self.cat_attrs = cat_attrs
        self.out_emb_dim = out_emb_dim
        if self.n_num > 0:
            self.mu = nn.Linear(cfg.d_model, self.n_num)
            self.log_sigma = nn.Linear(cfg.d_model, self.n_num)
        self.cat_proj = nn.ModuleList([nn.Linear(cfg.d_model, out_emb_dim) for _ in cat_attrs])
        self.cat_emb = nn.ModuleList([nn.Embedding(a.cardinality, out_emb_dim) for a in cat_attrs])

    def forward(self, H):
        out = {}
        if self.n_num > 0:
            # clamp log-sigma for numerical stability of the NLL
            out["num_mu"] = self.mu(H)
            out["num_log_sigma"] = self.log_sigma(H).clamp(-6.0, 6.0)
        cats = []
        for proj, emb, a in zip(self.cat_proj, self.cat_emb, self.cat_attrs):
            cats.append(dict(H_a=proj(H), E=emb.weight, cardinality=a.cardinality,
                             is_high_card=a.is_high_card, name=a.name))
        out["cats"] = cats
        return out


class TREASURE(nn.Module):
    def __init__(self, schema: Schema, cfg: ModelConfig):
        super().__init__()
        self.schema = schema
        self.cfg = cfg
        self.static_in = InputModule(schema.static_num, schema.static_cat, cfg)
        self.dynamic_in = InputModule(schema.dynamic_num, schema.dynamic_cat, cfg)
        self.blocks = nn.ModuleList([DecoderBlock(cfg) for _ in range(cfg.n_layers)])
        self.final_norm = nn.LayerNorm(cfg.d_model)
        # two output heads (share the identical sub-module design, separate weights)
        self.next_head = OutputSubModule(schema.dynamic_num, schema.dynamic_cat, cfg)
        self.signal_head = OutputSubModule(schema.signal_num, schema.signal_cat, cfg)

    def forward(self, batch):
        sn, sc = batch["static_num"], batch["static_cat"]         # [B,Sn] [B,Sc]
        dn, dc = batch["dyn_num"], batch["dyn_cat"]               # [B,L,Fn] [B,L,Fc]
        pad = batch["pad_mask"]                                    # [B,L] True=pad
        B, L, _ = dn.shape

        h_static = self.static_in(sn, sc).unsqueeze(1)            # [B,1,d]
        h_dyn = self.dynamic_in(dn, dc)                           # [B,L,d]
        x = torch.cat([h_static, h_dyn], dim=1)                  # [B,1+L,d]  static first

        S = L + 1
        # bool masks (True = disallow) for both attn & padding, to match dtypes
        causal = torch.triu(torch.ones(S, S, dtype=torch.bool, device=x.device), diagonal=1)
        # static column (index 0) is never padded
        kpm = torch.cat([torch.zeros(B, 1, dtype=torch.bool, device=x.device), pad], dim=1)

        for blk in self.blocks:
            x = blk(x, attn_mask=causal, key_padding_mask=kpm)
        x = self.final_norm(x)

        h_out = x[:, 1:, :]                                       # drop static position -> [B,L,d]
        return dict(next=self.next_head(h_out), signal=self.signal_head(h_out))


def build_model(schema: Schema, cfg: ModelConfig) -> TREASURE:
    return TREASURE(schema, cfg)


def count_params(m: nn.Module) -> int:
    return sum(p.numel() for p in m.parameters())
