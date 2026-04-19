# libtv-skill

## 3x3 面料看板入口

`scripts/generate_texture_collection_board.py` 是给 `auto-garment-producer` 使用的稳定生图入口。它会先切换到新 project，再创建新 session，只下载当前 session 返回的图片结果，不扫描历史目录。

成功输出以 `metadata.json` 为准：

- `selected_board_path`: 选中的合格 3x3 面料看板路径。
- `valid_board`: 是否选出合格看板。
- `downloaded_count`: 当前 session 已下载图片数量。
- `image_candidates`: 每张下载图的尺寸、比例和候选判断。
- `board_validation_policy`: 当前近似正方形判定策略。
- `last_status`: `succeeded` 表示合格看板已选中；`download_succeeded` 只表示下载完成；`no_valid_3x3_board` 表示下载到图片但没有合格看板。

该入口只生成面料看板/连续纹理小样，不生成正面成衣效果图、服装 mockup、模特上身图、假人图或产品照。
