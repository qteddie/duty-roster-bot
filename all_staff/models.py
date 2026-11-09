from __future__ import annotations

from typing import Iterable

from .schema import CHARSET


def _torch():
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:
        raise RuntimeError("請先安裝 requirements-train.txt") from exc
    return torch, nn


def build_detector():
    """Small U-Net-like foreground segmenter for personnel-code regions."""
    torch, nn = _torch()

    def block(in_channels, out_channels):
        return nn.Sequential(nn.Conv2d(in_channels, out_channels, 3, padding=1),
                             nn.BatchNorm2d(out_channels), nn.ReLU(inplace=True),
                             nn.Conv2d(out_channels, out_channels, 3, padding=1),
                             nn.BatchNorm2d(out_channels), nn.ReLU(inplace=True))

    class Detector(nn.Module):
        def __init__(self):
            super().__init__()
            self.e1, self.e2, self.e3 = block(1, 24), block(24, 48), block(48, 96)
            self.pool = nn.MaxPool2d(2)
            self.u2 = nn.ConvTranspose2d(96, 48, 2, 2)
            self.d2 = block(96, 48)
            self.u1 = nn.ConvTranspose2d(48, 24, 2, 2)
            self.d1 = block(48, 24)
            self.head = nn.Conv2d(24, 1, 1)

        def forward(self, x):
            a = self.e1(x); b = self.e2(self.pool(a)); c = self.e3(self.pool(b))
            x = self.d2(torch.cat((self.u2(c), b), 1))
            return self.head(self.d1(torch.cat((self.u1(x), a), 1)))
    return Detector()


def build_recognizer(charset: tuple[str, ...] = CHARSET):
    """CRNN recognizer; class 0 is the CTC blank token."""
    torch, nn = _torch()

    class Recognizer(nn.Module):
        def __init__(self):
            super().__init__()
            self.cnn = nn.Sequential(
                nn.Conv2d(1, 32, 3, padding=1), nn.ReLU(True), nn.MaxPool2d(2, 2),
                nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(True), nn.MaxPool2d(2, 2),
                nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(True),
                nn.MaxPool2d((2, 1)),
                nn.Conv2d(128, 192, 3, padding=1), nn.BatchNorm2d(192), nn.ReLU(True),
                nn.AdaptiveAvgPool2d((1, None)),
            )
            self.rnn = nn.LSTM(192, 128, num_layers=2, bidirectional=True, dropout=.1)
            self.head = nn.Linear(256, len(charset) + 1)

        def forward(self, x):
            features = self.cnn(x).squeeze(2).permute(2, 0, 1)
            sequence, _ = self.rnn(features)
            return self.head(sequence).log_softmax(2)
    return Recognizer()


def ctc_greedy_decode(log_probs, charset: tuple[str, ...] = CHARSET) -> list[tuple[str, float]]:
    torch, _ = _torch()
    probabilities = log_probs.exp()
    scores, indexes = probabilities.max(2)
    results = []
    for batch in range(indexes.shape[1]):
        previous, characters, confidences = 0, [], []
        for index, score in zip(indexes[:, batch].tolist(), scores[:, batch].tolist()):
            if index and index != previous:
                characters.append(charset[index - 1]); confidences.append(float(score))
            previous = index
        confidence = min(confidences) if confidences else 0.0
        results.append(("".join(characters), confidence))
    return results
