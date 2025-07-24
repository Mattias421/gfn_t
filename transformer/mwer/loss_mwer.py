import edit_distance
import torch

def mwer_loss(n_best_tokens, n_best_probs, tokens_eos):
    Z = n_best_probs.sum(dim=1)
    errors = torch.zeros_like(n_best_probs)

    L = tokens_eos.shape[1]
    B, N = n_best_probs.shape

    tokens_labels = tokens_eos[:, None, :].expand(B, N, L)
    breakpoint()
