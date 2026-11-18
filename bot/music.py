"""Generates an original, royalty-free background track with numpy (no copyright issues).

Each track is built from a seed, so different posts can get slightly different music:
the chord progression, tempo and drum pattern vary with the seed.
"""
import wave

import numpy as np

SR = 48000

# (root note, minor?) per bar
PROGRESSIONS = [
    [(57, True), (53, False), (48, False), (55, False)],   # Am  F   C   G
    [(50, True), (46, False), (53, False), (48, False)],   # Dm  Bb  F   C
    [(52, True), (48, False), (55, False), (50, False)],   # Em  C   G   D
    [(57, True), (55, False), (53, False), (52, False)],   # Am  G   F   E
]


def _midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def _env(n, attack, decay):
    t = np.arange(n) / SR
    e = np.exp(-t / decay)
    a = max(1, int(attack * SR))
    e[:a] *= np.linspace(0, 1, a)
    return e


def _saw(freq, n, harmonics=8):
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * freq * k * t) / k
    return out


def make_track(duration: float, path, seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    bpm = float(rng.choice([112, 116, 120, 124]))
    prog = PROGRESSIONS[seed % len(PROGRESSIONS)]
    beat = 60.0 / bpm
    total = int((duration + 1) * SR)
    mix = np.zeros(total)

    def add(sig, start_s, gain):
        i = int(start_s * SR)
        if i >= total:
            return
        seg = sig[: total - i]
        mix[i:i + len(seg)] += seg * gain

    # one-shot sounds
    kn = int(0.35 * SR)
    t = np.arange(kn) / SR
    kick = np.sin(2 * np.pi * (45 + 75 * np.exp(-t / 0.04)) * t) * _env(kn, 0.002, 0.12)
    hn = int(0.05 * SR)
    hat = np.diff(rng.standard_normal(hn + 1)) * _env(hn, 0.001, 0.012)
    cn = int(0.18 * SR)
    clap = (rng.standard_normal(cn) * 0.6 + np.sin(2 * np.pi * 190 * np.arange(cn) / SR)) * _env(cn, 0.001, 0.05)

    beats = int(duration / beat) + 2
    for b in range(beats):
        s = b * beat
        add(kick, s, 0.9)
        add(hat, s + beat / 2, 0.18)
        if b % 2 == 1:
            add(clap, s, 0.35)
        if rng.random() < 0.3:
            add(hat, s + beat / 4 * 3, 0.10)

    # bass + pad, one chord per bar (4 beats)
    bar = 4 * beat
    bars = int(duration / bar) + 2
    pad_len = int(bar * SR)
    for i in range(bars):
        root, minor = prog[i % len(prog)]
        start = i * bar
        chord = [root, root + (3 if minor else 4), root + 7, root + 12]
        pad = sum(_saw(_midi(n) * (1 + d), pad_len, 5) for n in chord for d in (-0.003, 0.003))
        pad *= np.minimum(1, np.linspace(0, 6, pad_len)) * np.minimum(1, np.linspace(6, 0, pad_len))
        add(pad / len(chord), start, 0.06)
        # bass: 8th-note pulses an octave below the root
        for k in range(8):
            bn = int(beat / 2 * SR)
            note = _saw(_midi(root - 12), bn, 6) * _env(bn, 0.005, 0.12)
            add(note, start + k * beat / 2, 0.22)

    mix = mix[: int(duration * SR)]
    mix /= max(1e-6, np.max(np.abs(mix))) / 0.85
    fade_in, fade_out = int(0.4 * SR), int(1.5 * SR)
    mix[:fade_in] *= np.linspace(0, 1, fade_in)
    mix[-fade_out:] *= np.linspace(1, 0, fade_out)

    stereo = np.stack([mix, np.roll(mix, int(0.012 * SR))], axis=1)
    pcm = (stereo * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return str(path)
