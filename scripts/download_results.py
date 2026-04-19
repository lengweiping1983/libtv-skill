#!/usr/bin/env python3
"""下载生成结果：从会话中提取所有图片/视频 URL 并批量下载到本地"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _common import query_session


URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\\])}，。；、]+")
MEDIA_EXT_PATTERN = re.compile(r"\.(?:png|jpg|jpeg|webp|gif|mp4|mov|webm)(?:\?|$)", re.IGNORECASE)
RESULT_URL_KEYS = {
    "url",
    "urls",
    "uri",
    "src",
    "href",
    "path",
    "preview",
    "preview_path",
    "previewPath",
    "image",
    "images",
    "image_url",
    "imageUrl",
    "image_urls",
    "imageUrls",
    "output",
    "outputs",
    "result",
    "results",
    "video",
    "videos",
    "video_url",
    "videoUrl",
}


def _clean_url(url: str) -> str:
    url = html.unescape(str(url)).strip()
    url = url.replace("\\/", "/")
    url = url.rstrip(".,;:!?，。；、")
    return url


def _looks_like_result_url(url: str) -> bool:
    lower = url.lower()
    if MEDIA_EXT_PATTERN.search(lower):
        return True
    return any(
        marker in lower
        for marker in (
            "libtv-res",
            "liblib.art",
            "liblibai-online",
            "aliyuncs",
            "cos.",
            "oss-",
            "oss.",
            "cdn.",
            "image",
            "img",
            "video",
            "media",
            "preview",
            "result",
        )
    )


def _maybe_parse_json(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _collect_urls(value, urls: list[str], *, key_hint: str = "") -> None:
    value = _maybe_parse_json(value)
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_urls(item, urls, key_hint=str(key))
        return
    if isinstance(value, list):
        for item in value:
            _collect_urls(item, urls, key_hint=key_hint)
        return
    if not isinstance(value, str):
        return

    text = html.unescape(value).replace("\\/", "/")
    direct = _clean_url(text)
    if direct.startswith(("http://", "https://")) and (
        key_hint in RESULT_URL_KEYS or _looks_like_result_url(direct)
    ):
        urls.append(direct)
    for match in URL_PATTERN.findall(text):
        url = _clean_url(match)
        if _looks_like_result_url(url):
            urls.append(url)


def extract_urls_from_messages(messages):
    """从会话消息中递归提取图片/视频结果 URL，兼容 tool JSON、assistant 文本和嵌套字段。"""
    urls = []
    for msg in messages or []:
        _collect_urls(msg, urls)

    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def download_file(url, filepath):
    """下载单个文件"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LibTV-Skill/1.0",
            "Accept": "image/*,video/*,*/*;q=0.8",
        },
    )
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = f"{filepath}.part"
    last_error = ""
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                status = getattr(resp, "status", 200)
                if status and status >= 400:
                    raise urllib.error.HTTPError(url, status, f"HTTP {status}", resp.headers, None)
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 64)
                        if not chunk:
                            break
                        f.write(chunk)
            os.replace(tmp_path, filepath)
            return filepath, None
        except Exception as e:
            last_error = str(e)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(1.5 * attempt)
    return filepath, last_error


def main():
    parser = argparse.ArgumentParser(
        description="下载会话中生成的图片/视频到本地",
        epilog="""
使用方式:
  # 从会话自动提取并下载所有结果
  python3 download_results.py SESSION_ID

  # 指定输出目录
  python3 download_results.py SESSION_ID --output-dir ~/Desktop/my_project

  # 指定文件名前缀
  python3 download_results.py SESSION_ID --prefix "storyboard"

  # 直接下载指定 URL 列表
  python3 download_results.py --urls URL1 URL2 URL3 --output-dir ./output
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("session_id", nargs="?", default="", help="会话 ID，自动提取该会话所有生成结果的 URL")
    parser.add_argument("--urls", nargs="+", default=[], help="直接指定要下载的 URL 列表（不需要 session_id）")
    parser.add_argument("--output-dir", default="", help="输出目录（默认 ~/Downloads/libtv_results/）")
    parser.add_argument("--prefix", default="", help="文件名前缀（如 'storyboard' → storyboard_01.png）")
    parser.add_argument("--workers", type=int, default=5, help="并行下载线程数（默认 5）")
    parser.add_argument("--after-seq", type=int, default=0, help="只拉取 seq 大于此值的消息（增量模式）。默认 0 表示拉取全部。")
    args = parser.parse_args()

    # 收集 URL
    urls = list(args.urls)
    if args.session_id:
        data = query_session(args.session_id, after_seq=args.after_seq)
        messages = data.get("messages", [])
        extracted = extract_urls_from_messages(messages)
        urls.extend(extracted)

    if not urls:
        print(json.dumps({"error": "未找到可下载的图片/视频 URL", "downloaded": []}, ensure_ascii=False, indent=2))
        sys.exit(1)

    # 准备输出目录
    output_dir = args.output_dir or os.path.expanduser("~/Downloads/libtv_results")
    os.makedirs(output_dir, exist_ok=True)

    # 构建下载任务
    tasks = []
    for i, url in enumerate(urls, 1):
        ext = os.path.splitext(url.split("?")[0])[-1] or ".png"
        if args.prefix:
            filename = f"{args.prefix}_{i:02d}{ext}"
        else:
            filename = f"{i:02d}{ext}"
        filepath = os.path.join(output_dir, filename)
        tasks.append((url, filepath))

    # 并行下载
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_file, url, fp): (url, fp) for url, fp in tasks}
        for future in as_completed(futures):
            fp, err = future.result()
            if err:
                errors.append({"file": fp, "error": err})
            else:
                results.append(fp)

    # 按文件名排序输出
    results.sort()

    output = {
        "output_dir": output_dir,
        "downloaded": results,
        "total": len(results),
    }
    if errors:
        output["errors"] = errors

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
