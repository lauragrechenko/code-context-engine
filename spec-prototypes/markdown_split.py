"""Prototype: fence-aware markdown heading splitter + size windowing."""
import math, os, re, subprocess, sys

WIN, OV = 1500, 200
FENCE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})")
ATX = re.compile(r"^(#{1,6})\s+\S")

def sections(text):
    """Yield (start_line, end_line, heading_path) for each ATX-heading section."""
    lines = text.split("\n")
    fence = None
    marks = []                       # (line_idx, level, title)
    for i, ln in enumerate(lines):
        m = FENCE.match(ln)
        if m:
            tok = m.group(2)
            if fence is None:
                fence = tok[0] * len(tok)
            elif ln.strip().startswith(fence[0] * 3):
                fence = None
            continue
        if fence is not None:
            continue
        h = ATX.match(ln)
        if h:
            marks.append((i, len(h.group(1)), ln.strip("# ").strip()))
    if not marks or marks[0][0] > 0:
        marks.insert(0, (0, 0, "(preamble)"))
    out, stack = [], []
    for n, (i, lvl, title) in enumerate(marks):
        end = marks[n + 1][0] - 1 if n + 1 < len(marks) else len(lines) - 1
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        path = " > ".join(t for _, t in stack + [(lvl, title)])
        stack.append((lvl, title))
        out.append((i, end, path))
    return lines, out

def windows(n):
    return max(1, math.ceil(max(0, n - OV) / (WIN - OV)))

root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
files = [f for f in subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=root).stdout.split() if f.endswith(".md")]
files += [f for f in ("CLAUDE.md", "AGENTS.md") if os.path.exists(os.path.join(root, f)) and f not in files]
tot = chunks = cov = over = 0
big = []
for f in files:
    text = open(os.path.join(root, f)).read()
    tot += len(text)
    lines, secs = sections(text)
    for a, b, path in secs:
        body = "\n".join(lines[a:b + 1])
        cov += len(body)
        w = windows(len(body))
        chunks += w
        if w > 1:
            over += 1
            big.append((len(body), f, path[:60]))
print(f"markdown files={len(files)} bytes={tot:,}")
print(f"sections={sum(len(sections(open(os.path.join(root,f)).read())[1]) for f in files)} chunks after windowing={chunks} oversize sections={over}")
print(f"coverage={100*cov/tot:.1f}%")
big.sort(reverse=True)
print("largest sections:")
for n, f, p in big[:6]:
    print(f"  {n:>6}B {f}  [{p}]")
