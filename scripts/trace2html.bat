@echo off
rem trace2html：把 trace jsonl 拖到这个文件上 → 转成易读 HTML 并自动打开浏览器。
rem 支持拖入单个 jsonl 或整个 traces 目录（目录会生成 index.html 索引）。
rem 无参数双击时弹文件选择框。

uv run python -m kdagent.eval.trace_html %*

if errorlevel 1 pause
