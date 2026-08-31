"""
TREASURE configuration and attribute schema.

Copied unchanged from the MCB reimplementation of arXiv:2511.19693, alongside
`model.py`, so the package can be imported and tested here rather than only
inside an assembled Kaggle kernel. The defaults below describe the paper's own
synthetic schema; `sequences.py` overrides all of them with the IEEE-CIS
mapping.

This mirrors the paper's data model (Section 3.1):
  - Each card has ONE static attribute vector (X_s) and t dynamic vectors (X_d,i).
  - Every attribute vector holds numerical (float) + categorical (int index) attributes.
  - Two output sets per step: NEXT-transaction attributes and CURRENT network signals.

The paper uses 5 static / 16 dynamic / 2 network-signal attributes on proprietary Visa
data. We reproduce the same *structure* on synthetic data we can share and train on.
"""
from dataclasses import dataclass, field
from typing import List, Dict


# Cardinality threshold separating low- vs high-cardinality categoricals (paper: 1024).
HIGH_CARD_THRESHOLD = 1024


@dataclass
class CatAttr:
    """A categorical attribute definition."""
    name: str
    cardinality: int                 # number of distinct category indices [0, cardinality-1]

    @property
    def is_high_card(self) -> bool:
        return self.cardinality > HIGH_CARD_THRESHOLD


@dataclass
class NumAttr:
    """A numerical attribute definition (stored raw; log-transformed inside the model)."""
    name: str


@dataclass
class Schema:
    """Full attribute schema shared by the data generator, dataset and model."""
    # ----- STATIC (per-card, constant across the sequence) -----
    static_num: List[NumAttr] = field(default_factory=lambda: [
        NumAttr("card_tenure_days"),          # how long the card has existed
        NumAttr("credit_limit"),
    ])
    static_cat: List[CatAttr] = field(default_factory=lambda: [
        CatAttr("card_product", 6),           # classic / gold / platinum / signature / infinite / business
        CatAttr("issuer_bank", 64),
        CatAttr("card_country", 40),
    ])

    # ----- DYNAMIC (per-transaction) -----
    dynamic_num: List[NumAttr] = field(default_factory=lambda: [
        NumAttr("amount"),
        NumAttr("time_delta"),                # seconds since previous txn on this card
    ])
    dynamic_cat: List[CatAttr] = field(default_factory=lambda: [
        CatAttr("merchant_id", 50_000),       # HIGH cardinality -> InfoNCE
        CatAttr("merchant_city", 4_000),      # HIGH cardinality -> InfoNCE
        CatAttr("merchant_category", 300),     # MCC
        CatAttr("merchant_country", 200),
        CatAttr("channel", 5),                # pos / online / atm / p2p / recurring
        CatAttr("entry_mode", 6),
        CatAttr("hour_of_day", 24),
        CatAttr("day_of_week", 7),
    ])

    # ----- NETWORK SIGNALS (outputs for the CURRENT txn; known only after processing) -----
    signal_num: List[NumAttr] = field(default_factory=list)
    signal_cat: List[CatAttr] = field(default_factory=lambda: [
        CatAttr("response_code", 10),         # approved / various decline reasons
        CatAttr("abnormal_flag", 2),          # THE critical attribute (Eq. 4 pivots on it)
    ])

    # The single most important attribute; loss aggregation is anchored on it.
    abnormal_attr_name: str = "abnormal_flag"

    # ---- convenience accessors ----
    def all_static(self):
        return self.static_num, self.static_cat

    def all_dynamic(self):
        return self.dynamic_num, self.dynamic_cat

    def all_signal(self):
        return self.signal_num, self.signal_cat


@dataclass
class ModelConfig:
    d_model: int = 256          # transformer hidden dim (paper: 256)
    n_layers: int = 3           # transformer decoder layers (paper: 3)
    n_heads: int = 4            # attention heads (paper: 4)
    input_layers: int = 3       # input-module linear+GELU blocks (paper: 3)
    dropout: float = 0.1
    cat_emb_dim: int = 32       # per-categorical embedding width inside input module
    n_negatives: int = 1024     # shared negatives for high-card InfoNCE (paper: 1024/batch)
    max_seq_len: int = 512      # cap on transactions per card (paper: 512)


@dataclass
class TrainConfig:
    lr: float = 1e-4            # AdamW (paper)
    weight_decay: float = 1e-2
    batch_size: int = 256       # paper: 256 (auto-reduced if GPU memory is tight)
    epochs: int = 20            # paper: 20
    grad_clip: float = 1.0
    amp: bool = True            # mixed precision to fit a 6 GB laptop GPU
    num_workers: int = 0
    seed: int = 42
