#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_md2html.py —— 把 STRATEGY.md 转成自包含 HTML（浅色主题，A4 友好）

用法：python _md2html.py [输入.md] [输出.html]
"""
import os
import sys

import markdown

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "STRATEGY.md")
DST = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "STRATEGY.html")

with open(SRC, encoding="utf-8") as f:
    md_text = f.read()

body = markdown.markdown(
    md_text,
    extensions=["tables", "fenced_code", "toc", "sane_lists"],
    extension_configs={"toc": {"toc_depth": "2-3"}},
)

CSS = """
:root{
  --ink:#1a1d21; --ink2:#3f4650; --ink3:#6b7481;
  --line:#e3e6ea; --line2:#eef1f4;
  --bg:#ffffff; --bg2:#f7f8fa; --bg3:#f2f4f7;
  --accent:#1f4e79; --accent2:#2d6ca3;
  --warn:#a8321f; --warnbg:#fdf3f1;
  --ok:#1e6b45; --okbg:#f2f8f4;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:0; background:var(--bg2);
  font-family:"Georgia","Songti SC","Source Han Serif SC","Noto Serif CJK SC",serif;
  color:var(--ink); line-height:1.78; font-size:15.5px;
}
.wrap{max-width:900px; margin:0 auto; background:var(--bg);
  padding:56px 64px 80px; box-shadow:0 1px 24px rgba(20,30,45,.07);}
h1,h2,h3,h4{
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  color:var(--ink); line-height:1.35; font-weight:600;
}
h1{font-size:29px; margin:8px 0 6px; letter-spacing:-.3px;}
h1+h2{margin-top:14px}
h2{font-size:20.5px; margin:44px 0 14px; padding-bottom:9px;
   border-bottom:2px solid var(--line); letter-spacing:-.2px;}
h2::before{content:""; display:inline-block; width:4px; height:17px;
  background:var(--accent); margin-right:11px; vertical-align:-2px; border-radius:1px;}
h3{font-size:16.5px; margin:30px 0 10px; color:var(--accent);}
h4{font-size:15px; margin:22px 0 8px; color:var(--ink2);}
p{margin:11px 0;}
a{color:var(--accent2);}
strong{color:var(--ink); font-weight:600;}
em{color:var(--ink2);}
hr{border:0; border-top:1px solid var(--line); margin:34px 0;}
blockquote{
  margin:18px 0; padding:13px 20px; background:var(--bg3);
  border-left:3px solid var(--accent2); color:var(--ink2); font-size:14.5px;
}
blockquote p{margin:5px 0;}
blockquote h2,blockquote h3{border:0; margin:6px 0;}
table{
  border-collapse:collapse; width:100%; margin:16px 0 20px;
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:13.6px; line-height:1.55;
}
thead th{
  background:var(--bg3); color:var(--ink); font-weight:600;
  text-align:left; padding:9px 11px; border-bottom:2px solid #cfd6de;
  white-space:nowrap;
}
tbody td{padding:8px 11px; border-bottom:1px solid var(--line2); vertical-align:top;}
tbody tr:nth-child(even){background:#fbfcfd;}
tbody tr:hover{background:#f5f8fb;}
table code{font-size:12.5px;}
code{
  font-family:"Cascadia Mono","Consolas","SF Mono",monospace;
  background:var(--bg3); padding:1.5px 5px; border-radius:3px;
  font-size:13px; color:#8a3b2c;
}
pre{
  background:#1e2430; color:#e6e9ef; padding:15px 18px; border-radius:5px;
  overflow-x:auto; font-size:13px; line-height:1.65; margin:16px 0;
}
pre code{background:none; color:inherit; padding:0; font-size:13px;}
ul,ol{margin:11px 0; padding-left:26px;}
li{margin:5px 0;}
li>ul,li>ol{margin:4px 0;}
.header-meta{
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:13px; color:var(--ink3); margin:0 0 26px;
  padding-bottom:16px; border-bottom:1px solid var(--line);
}
.warn{
  background:var(--warnbg); border-left:3px solid var(--warn);
  padding:12px 18px; margin:16px 0; font-size:14.2px; color:#5a2620;
}
@media print{
  body{background:#fff; font-size:10.5pt;}
  .wrap{box-shadow:none; padding:0; max-width:none;}
  h2{page-break-after:avoid;}
  table,pre,blockquote{page-break-inside:avoid;}
}
@media (max-width:720px){
  .wrap{padding:28px 20px 50px;}
  h1{font-size:23px;} h2{font-size:18px;}
  table{font-size:12.2px;}
  thead th,tbody td{padding:6px 7px;}
}
"""

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>加密趋势策略说明书</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""

with open(DST, "w", encoding="utf-8") as f:
    f.write(html)

print(f"已生成 {DST}（{len(html)/1024:.1f} KB）")
print(f"源文件 {SRC}")
