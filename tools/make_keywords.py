"""Keywords files for the sherpa-onnx zh-en KWS model, one per wake word (ride_logic.WAKE_WORDS): "HEY HERMES" (the
public default) and "HEY LANA", each with the plain name too, as the model's own phone tokens.

The zh-en 3M model spells English with ARPAbet phones from its lexicon (en.phone), so no sentencepiece/text2token
is needed: each word is looked up, every lexicon pronunciation plus EXTRA variants is combined, and each line is
checked against tokens.txt. Plain "LANA" gets a stricter per-keyword threshold (#) than the two-word phrase.

    python3 tools/make_keywords.py <model_dir> static/vendor     # writes kws-words-<wake word>.txt
"""
import itertools
import os
import sys

PHRASES = [('HEY LANA', None), ('LANA', 0.35)]           # (phrase, per-keyword threshold or None = global)
EXTRA = {'LANA': [['L', 'AA1', 'N', 'AH0']]}             # "LAH-na" as well as the lexicon's "LAN-a"
# file name -> (phrases, extra pronunciations); names match ride_logic.WAKE_WORDS[...]['keywords']
SETS = {
    'kws-words-hey-hermes.txt': ([('HEY HERMES', None), ('HERMES', 0.35)], {}),
    'kws-words-hey-lana.txt': (PHRASES, EXTRA),
}


def load_lexicon(path, words):
    lex = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            w = parts[0].split('(')[0].upper()
            if w in words:
                lex.setdefault(w, []).append(parts[1:])
    return lex


def load_tokens(path):
    with open(path, encoding='utf-8') as f:
        return {line.split()[0] for line in f if line.split()}


def keyword_lines(lexicon, tokens, phrases=PHRASES, extra=EXTRA):
    out = []
    for phrase, thr in phrases:
        words = phrase.split()
        options = []
        for w in words:
            prons = [p for p in lexicon.get(w, [])] + [p for p in extra.get(w, []) if p not in lexicon.get(w, [])]
            if not prons:
                raise SystemExit(f'no pronunciation for {w!r}')
            options.append(prons)
        for combo in itertools.product(*options):
            toks = [t for pron in combo for t in pron]
            missing = [t for t in toks if t not in tokens]
            if missing:
                raise SystemExit(f'{phrase}: tokens not in the model: {missing}')
            line = ' '.join(toks)
            if thr is not None:
                line += f' #{thr}'
            out.append(f"{line} @{phrase.replace(' ', '_')}")
    return out


def write_all(model_dir, out_dir):
    tokens = load_tokens(os.path.join(model_dir, 'tokens.txt'))
    for name, (phrases, extra) in SETS.items():
        words = {w for p, _ in phrases for w in p.split()}
        lines = keyword_lines(load_lexicon(os.path.join(model_dir, 'en.phone'), words), tokens, phrases, extra)
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'{name}: {len(lines)} lines')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('usage: make_keywords.py <model_dir> <out_dir>')
    write_all(sys.argv[1], sys.argv[2])
