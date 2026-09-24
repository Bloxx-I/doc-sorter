"""PaddleOCR helper; runs inside paddle-venv (Python 3.12) because Paddle has no 3.14 wheels.

Usage: paddle-venv/bin/python pipeline/paddle_worker.py page1.png page2.png ...
Prints a JSON list with one text string per image.
"""

import json
import os
import sys

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "HuggingFace")


def main(paths):
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(lang="de", use_doc_orientation_classify=False,
                    use_doc_unwarping=False, use_textline_orientation=False)
    texts = []
    for path in paths:
        lines = []
        for result in ocr.predict(path):
            lines.extend(result.get("rec_texts") or [])
        texts.append("\n".join(lines))
    sys.stdout.write("\n@@RESULT@@" + json.dumps(texts, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
