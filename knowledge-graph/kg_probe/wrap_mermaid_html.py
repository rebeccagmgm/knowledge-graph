"""Wrap a Mermaid source file in a locally openable HTML page."""

from __future__ import annotations

import argparse
import html
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = html.escape(args.source.read_text(encoding="utf-8"), quote=False)
    document = f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>176827 table lineage</title>
  <style>
    html, body {{ margin: 0; min-height: 100%; background: #f8fafc; }}
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; overflow: auto; }}
    .toolbar {{ position: sticky; top: 0; z-index: 2; padding: 12px 16px; background: #ffffff; border-bottom: 1px solid #cbd5e1; color: #334155; }}
    .hint {{ font-size: 13px; }}
    .canvas {{ padding: 16px; min-width: 1200px; }}
    .mermaid {{ min-width: 1200px; }}
  </style>
</head>
<body>
  <div class="toolbar"><strong>176827 完整表级上游图</strong><span class="hint">　可滚动查看；如果图未出现，请确认浏览器允许加载 Mermaid CDN。</span></div>
  <main class="canvas">
    <pre class="mermaid">{source}</pre>
  </main>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({{ startOnLoad: true, securityLevel: "loose", flowchart: {{ useMaxWidth: false, htmlLabels: true, curve: "basis" }} }});
  </script>
</body>
</html>
'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
