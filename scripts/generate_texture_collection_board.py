#!/usr/bin/env python3
"""
libtv-skill - Commercial textile collection board automation.

Creates a 3x3 coordinated textile collection board through libtv agent-im.
This script mirrors the Neo AI collection-board CLI shape so upstream skills can
call libtv through one stable entrypoint instead of hand-assembling
create/query/download steps.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


DEFAULT_PROMPT = """请生成一张 3x3 商业面料看板图片：九个协调的面料面板在一个正方形图片中等分排列，面板之间有细白色间隔。第一行和第二行必须是可铺满面料的连续纹理小样，第三行是干净浅色背景上的定位图案，方便后期去背景。只生成面料九宫格看板、连续纹理小样和干净定位图案。不要正面成衣效果图、不要服装 mockup、不要模特上身图、不要假人、不要产品照、不要 lookbook。图片中不要文字、标签、标题、logo、水印。"""

DEFAULT_NEGATIVE_PROMPT = """正面成衣效果图, 服装mockup, 模特上身图, 假人, 人物, 人脸, T恤产品图, 商品摄影, lookbook, poster, sticker sheet, text, labels, logo, watermark"""

BOARD_VALIDATION_POLICY = {
    "type": "near_square_3x3_board_v1",
    "min_aspect_ratio": 0.92,
    "max_aspect_ratio": 1.08,
    "min_short_side_px": 512,
    "selection": "largest_valid_area_then_earliest_return",
}


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def write_metadata(output_dir: Path, metadata: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def record_event(output_dir: Path, metadata: dict, status: str, message: str = "", **extra) -> None:
    event = {
        "status": status,
        "message": message,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    event.update({k: v for k, v in extra.items() if v not in (None, "")})
    metadata.setdefault("events", []).append(event)
    metadata["last_status"] = status
    write_metadata(output_dir, metadata)


def _load_libtv_modules(access_key: str):
    if access_key:
        os.environ["LIBTV_ACCESS_KEY"] = access_key
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir))
    from _common import build_project_url, change_project, create_session, query_session
    from download_results import download_file, extract_urls_from_messages
    return build_project_url, change_project, create_session, query_session, download_file, extract_urls_from_messages


def _file_ext_from_url(url: str, fallback: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return suffix
    return f".{fallback.lstrip('.')}"


def _download_urls(urls: list[str], output_dir: Path, prefix: str, output_format: str, download_file, start_index: int = 1) -> list[dict]:
    images = []
    for index, url in enumerate(urls, start_index):
        ext = _file_ext_from_url(url, output_format)
        path = output_dir / f"{prefix}_{index:02d}{ext}"
        saved_path, err = download_file(url, str(path))
        if err:
            raise RuntimeError(f"下载失败: {url}: {err}")
        images.append({"filename": Path(saved_path).name, "path": str(Path(saved_path).resolve()), "url": url})
    return images


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read image dimensions without making Pillow a hard dependency."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except ImportError:
        pass

    # Minimal PNG support for environments that only have the stdlib.
    with path.open("rb") as f:
        header = f.read(24)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR":
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        return width, height
    raise RuntimeError(f"无法读取图片尺寸: {path}")


def _annotate_image_candidates(images: list[dict]) -> list[dict]:
    candidates = []
    for index, image in enumerate(images, 1):
        item = dict(image)
        item["index"] = index
        path = Path(str(image.get("path", "")))
        try:
            width, height = _image_dimensions(path)
            ratio = width / max(1, height)
            min_side = min(width, height)
            area = width * height
            is_square = (
                BOARD_VALIDATION_POLICY["min_aspect_ratio"]
                <= ratio
                <= BOARD_VALIDATION_POLICY["max_aspect_ratio"]
                and min_side >= BOARD_VALIDATION_POLICY["min_short_side_px"]
            )
            item.update({
                "width": width,
                "height": height,
                "aspect_ratio": round(ratio, 4),
                "area": area,
                "is_square_candidate": is_square,
                "validation_message": "ok" if is_square else (
                    f"not_square_3x3_candidate: {width}x{height}, ratio={ratio:.3f}"
                ),
            })
        except Exception as exc:
            item.update({
                "is_square_candidate": False,
                "validation_message": f"dimension_read_failed: {exc}",
            })
        candidates.append(item)
    return candidates


def _select_board_candidate(candidates: list[dict]) -> dict | None:
    valid = [item for item in candidates if item.get("is_square_candidate")]
    if not valid:
        return None
    # Prefer the largest valid square board from this request.
    return sorted(valid, key=lambda item: (item.get("area", 0), -item.get("index", 0)), reverse=True)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a 3x3 commercial textile collection board with libtv-skill.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Collection board prompt/description.")
    parser.add_argument("--prompt-file", help="Read prompt from a UTF-8 text file.")
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE_PROMPT)
    parser.add_argument("--negative-prompt-file", help="Read negative prompt from a UTF-8 text file.")
    parser.add_argument("--output-dir", default="./output/libtv_texture_collection_board")
    parser.add_argument("--output-format", default="png", choices=["jpeg", "jpg", "png", "webp"])
    parser.add_argument("--prefix", default="collection_board")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--poll-interval", type=int, default=8)
    parser.add_argument("--access-key", "--libtv-key", dest="access_key")
    args = parser.parse_args()

    if args.prompt_file:
        args.prompt = read_text(args.prompt_file)
    if args.negative_prompt_file:
        args.negative_prompt = read_text(args.negative_prompt_file)

    access_key = args.access_key or os.environ.get("LIBTV_ACCESS_KEY", "")
    if not access_key:
        print("Error: LIBTV_ACCESS_KEY is required via environment variable or --access-key.", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "provider": "libtv-skill",
        "prompt": args.prompt,
        "negative_prompt": args.negative_prompt,
        "output_format": args.output_format,
        "images": [],
        "image_candidates": [],
        "downloaded_count": 0,
        "ignored_url_count": 0,
        "ignored_url_reasons": [],
        "valid_board": False,
        "board_validation_policy": BOARD_VALIDATION_POLICY,
        "events": [],
    }
    write_metadata(output_dir, metadata)

    try:
        build_project_url, change_project, create_session, query_session, download_file, extract_urls_from_messages = _load_libtv_modules(access_key)

        message = (
            f"{args.prompt}\n\n"
            f"硬性反向约束：{args.negative_prompt}\n\n"
            "输出要求：只返回一张正方形 3x3 面料九宫格看板图片；不要生成正面成衣效果图、模特图、假人图或商品照片。"
        )

        # 先切换到新项目，确保本次任务完全隔离历史
        record_event(output_dir, metadata, "change_project_started", "创建/切换到新 libtv project")
        project_data = change_project()
        project_uuid = project_data.get("projectUuid", "")
        if not project_uuid:
            record_event(output_dir, metadata, "change_project_failed", "change_project 未返回 projectUuid", error_type="libtv_change_project_no_project_uuid")
            print("Error: change_project did not return projectUuid.", file=sys.stderr)
            return 1
        project_url = build_project_url(project_uuid)
        metadata.update({"projectUuid": project_uuid, "requestedProjectUuid": project_uuid, "projectUrl": project_url})
        record_event(output_dir, metadata, "change_project_succeeded", "已切换到新 project", project_uuid=project_uuid, project_url=project_url)

        record_event(output_dir, metadata, "create_session_started", "创建 libtv 会话并发送面料看板请求")
        session_data = create_session(session_id="", message=message)
        session_id = session_data.get("sessionId", "")
        session_project_uuid = session_data.get("projectUuid", "") or project_uuid
        project_url = session_data.get("projectUrl", "") or build_project_url(session_project_uuid)
        metadata.update({"projectUuid": session_project_uuid, "requestedProjectUuid": project_uuid, "sessionId": session_id, "projectUrl": project_url})
        if session_project_uuid != project_uuid:
            record_event(
                output_dir,
                metadata,
                "project_mismatch_warning",
                "create_session 返回的 projectUuid 与刚切换的 projectUuid 不一致，保留 create_session 实际值",
                requested_project_uuid=project_uuid,
                actual_project_uuid=session_project_uuid,
                project_url=project_url,
            )
        if not session_id:
            record_event(output_dir, metadata, "create_session_failed", "create_session 未返回 sessionId")
            print("Error: create_session did not return sessionId.", file=sys.stderr)
            return 1
        record_event(output_dir, metadata, "create_session_succeeded", "libtv 会话已创建", session_id=session_id, project_uuid=session_project_uuid, requested_project_uuid=project_uuid, project_url=project_url)

        record_event(output_dir, metadata, "polling_started", "开始轮询 libtv 图片结果", session_id=session_id, project_uuid=project_uuid)
        started = time.time()
        after_seq = 0
        seen_urls: set[str] = set()
        images: list[dict] = []
        ignored_url_reasons: list[dict] = []
        selected: dict | None = None
        while time.time() - started < args.timeout:
            time.sleep(max(1, args.poll_interval))
            data = query_session(session_id, after_seq=after_seq)
            messages = data.get("messages", [])
            for msg in messages:
                seq = msg.get("seq", msg.get("sequence", 0))
                if isinstance(seq, int):
                    after_seq = max(after_seq, seq)
            found = extract_urls_from_messages(messages)
            image_urls = []
            for url in found:
                if url in seen_urls:
                    ignored_url_reasons.append({"url": url, "reason": "duplicate_url"})
                    continue
                # This entrypoint only accepts image assets as 3x3 board candidates.
                suffix = Path(url.split("?", 1)[0]).suffix.lower()
                if suffix in {".mp4", ".mov", ".webm"}:
                    ignored_url_reasons.append({"url": url, "reason": "video_url_not_board_candidate"})
                    seen_urls.add(url)
                    continue
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    ignored_url_reasons.append({"url": url, "reason": f"unsupported_media_suffix:{suffix or 'none'}"})
                    seen_urls.add(url)
                    continue
                seen_urls.add(url)
                image_urls.append(url)

            if ignored_url_reasons:
                metadata.update({
                    "ignored_url_count": len(ignored_url_reasons),
                    "ignored_url_reasons": ignored_url_reasons,
                })
                write_metadata(output_dir, metadata)

            if not image_urls:
                continue

            record_event(output_dir, metadata, "result_detected", f"检测到 {len(image_urls)} 个新图片 URL", result_url_count=len(image_urls), after_seq=after_seq)
            record_event(output_dir, metadata, "download_started", f"开始下载 {len(image_urls)} 个图片结果")
            start_index = len(images) + 1
            downloaded = _download_urls(image_urls, output_dir, args.prefix, args.output_format, download_file, start_index=start_index)
            images.extend(downloaded)
            candidates = _annotate_image_candidates(images)
            selected = _select_board_candidate(candidates)
            metadata.update({
                "images": images,
                "image_candidates": candidates,
                "downloaded_count": len(images),
                "ignored_url_count": len(ignored_url_reasons),
                "ignored_url_reasons": ignored_url_reasons,
                "valid_board": bool(selected),
            })
            if selected:
                metadata.update({
                    "selected_board_path": selected.get("path", ""),
                    "selected_board_url": selected.get("url", ""),
                    "selected_board_index": selected.get("index"),
                })
            write_metadata(output_dir, metadata)
            record_event(output_dir, metadata, "download_succeeded", f"已下载 {len(images)} 个当前会话图片结果", downloaded_count=len(images))

            if selected:
                record_event(
                    output_dir,
                    metadata,
                    "board_candidate_selected",
                    "已选出合格 3x3 面料看板",
                    output_path=selected.get("path", ""),
                    selected_board_index=selected.get("index"),
                    selected_board_url=selected.get("url", ""),
                    width=selected.get("width"),
                    height=selected.get("height"),
                    aspect_ratio=selected.get("aspect_ratio"),
                )
                break

            record_event(output_dir, metadata, "no_valid_3x3_candidate_yet", "当前批次没有合格 3x3 方形看板，继续轮询", downloaded_count=len(images), after_seq=after_seq)

        if not images:
            record_event(output_dir, metadata, "poll_timeout", f"轮询超时（{args.timeout}s），未检测到图片结果", error_type="libtv_no_image_result", after_seq=after_seq)
            print(f"Error: polling timed out after {args.timeout}s without image result.", file=sys.stderr)
            return 1

        if not selected:
            metadata.update({
                "valid_board": False,
                "images": images,
                "image_candidates": _annotate_image_candidates(images),
                "downloaded_count": len(images),
                "ignored_url_count": len(ignored_url_reasons),
                "ignored_url_reasons": ignored_url_reasons,
            })
            write_metadata(output_dir, metadata)
            record_event(output_dir, metadata, "no_valid_3x3_board", f"已下载 {len(images)} 张图片，但没有合格 3x3 方形看板", error_type="libtv_no_valid_3x3_board", downloaded_count=len(images))
            print(f"Error: downloaded {len(images)} image(s), but no valid square 3x3 board was found.", file=sys.stderr)
            return 1

        record_event(output_dir, metadata, "succeeded", "libtv 面料看板已生成、下载并通过 3x3 校验", output_path=selected["path"], downloaded_count=len(images))
        print(json.dumps({
            "output_dir": str(output_dir.resolve()),
            "images": images,
            "image_candidates": metadata.get("image_candidates", []),
            "valid_board": True,
            "selected_board_path": selected.get("path", ""),
            "selected_board_url": selected.get("url", ""),
            "selected_board_index": selected.get("index"),
            "downloaded_count": len(images),
            "ignored_url_count": len(ignored_url_reasons),
            "ignored_url_reasons": ignored_url_reasons,
            "board_validation_policy": BOARD_VALIDATION_POLICY,
            "sessionId": session_id,
            "projectUuid": session_project_uuid,
            "requestedProjectUuid": project_uuid,
            "projectUrl": project_url,
            "metadata": str((output_dir / "metadata.json").resolve()),
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        record_event(output_dir, metadata, "failed", str(exc), error_type=type(exc).__name__)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
