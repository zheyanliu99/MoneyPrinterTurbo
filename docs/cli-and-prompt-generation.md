# CLI 和 Prompt 生成指南

这份文档用于绕过 WebUI，直接通过后端 CLI 生成视频，并给出可复用的脚本、关键词和 Prompt 模板。

## 快速结论

- 后端入口是 `cli.py`。
- 长脚本建议使用 `--video-script-file`。
- 长关键词列表建议使用 `--video-terms-file`。
- 开启 `--match-materials-to-script` 后，每一句字幕对应一个关键词和一个素材。
- 逐句匹配模式下，关键词数量必须和脚本句子数量一致。
- 中文脚本会自动使用中文 TTS voice，避免误用英文 voice。
- 在线素材源更适合英文搜索词；中文关键词会被转换为英文 stock-video search terms。
- Pexels 更适合具体城市和竖屏短视频；Coverr 横屏素材更多，但具体中国城市结果可能较少。

## 基础命令

在项目根目录执行：

```bash
.venv/bin/python cli.py --video-subject "你的主题"
```

查看所有参数：

```bash
.venv/bin/python cli.py --help
```

## 推荐的文件输入方式

创建脚本文件：

```bash
cat > /tmp/video_script.txt <<'EOF'
第五广州常住人口约1898万人。
面积约7434平方公里。
2024年GDP约3.10万亿元。
千年商都广州把烟火气炼成硬实力。
EOF
```

创建关键词文件：

```bash
cat > /tmp/video_terms.txt <<'EOF'
广州城市天际线，广州航拍城市，广州CBD天际线，广州小蛮腰
EOF
```

生成视频：

```bash
.venv/bin/python cli.py \
  --task-id china-city-demo \
  --video-subject "中国GDP城市排行榜" \
  --video-script-file /tmp/video_script.txt \
  --video-terms-file /tmp/video_terms.txt \
  --video-source pexels \
  --video-aspect 9:16 \
  --video-concat-mode sequential \
  --video-transition-mode none \
  --video-clip-duration 3 \
  --match-materials-to-script \
  --voice-name zh-CN-XiaoxiaoNeural-Female \
  --voice-rate 1.0 \
  --voice-volume 1.0 \
  --bgm-type none \
  --subtitle-enabled \
  --font-name MicrosoftYaHeiBold.ttc \
  --subtitle-position bottom \
  --font-size 60 \
  --stroke-color '#000000' \
  --stroke-width 1.5
```

生成结果会在：

```text
storage/tasks/<task-id>/final-1.mp4
```

任务调试信息会在：

```text
storage/tasks/<task-id>/script.json
```

`script.json` 里会包含 `matched_segments`，用于检查每句字幕对应了哪个关键词和素材。

## 本地素材 Smoke Test

如果只想测试后端链路，不想依赖 Pexels、Pixabay 或 Coverr：

```bash
.venv/bin/python cli.py \
  --task-id local-smoke-test \
  --video-subject "local smoke test" \
  --video-script "New York skyline wakes up in the morning. Central Park gives the city a quiet green heart." \
  --video-terms "New York skyline, Central Park" \
  --video-source local \
  --video-materials "1.png.mp4,2.png.mp4" \
  --video-aspect 9:16 \
  --video-concat-mode sequential \
  --video-transition-mode none \
  --video-clip-duration 3 \
  --match-materials-to-script \
  --voice-name en-AU-NatashaNeural-Female \
  --bgm-type none \
  --subtitle-enabled \
  --font-name MicrosoftYaHeiBold.ttc
```

本地素材必须放在：

```text
storage/local_videos/
```

## 分阶段调试

可以用 `--stop-at` 让任务停在某个阶段：

```bash
--stop-at script
--stop-at terms
--stop-at audio
--stop-at subtitle
--stop-at materials
--stop-at video
```

常见用法：

```bash
.venv/bin/python cli.py \
  --task-id debug-terms \
  --video-subject "纽约旅游景点介绍" \
  --video-script-file /tmp/video_script.txt \
  --video-source pexels \
  --match-materials-to-script \
  --stop-at terms
```

注意：`--video-source local` 不会生成在线搜索关键词，所以不能配 `--stop-at terms`。

## 逐句匹配规则

开启：

```bash
--match-materials-to-script
```

后端会按字幕时间轴生成 segment：

```json
{
  "index": 1,
  "text": "第五广州常住人口约1898万人",
  "term": "Guangzhou city skyline",
  "start": 0.1,
  "end": 3.913,
  "duration": 3.813,
  "material": "storage/cache_videos/xxx.mp4",
  "provider": "pexels"
}
```

这意味着：

- 第一句字幕只使用第一个关键词。
- 第一个素材持续到第一句字幕结束。
- 画面时长来自字幕 `start -> end`，不是固定 `video_clip_duration`。
- `video_clip_duration` 只作为旧模式或降级路径的最大切片时长。

## 中文视频建议

中文脚本建议一行一句，避免一句太长：

```text
第五广州常住人口约1898万人。
面积约7434平方公里。
2024年GDP约3.10万亿元。
千年商都广州把烟火气炼成硬实力。
```

关键词可以写中文：

```text
广州城市天际线，广州航拍城市，广州CBD天际线，广州小蛮腰
```

在线素材搜索时，系统会转换成英文，例如：

```text
Guangzhou city skyline
Guangzhou aerial city
Guangzhou CBD skyline
Guangzhou Canton Tower
```

如果你手动写英文关键词，通常素材搜索更稳定：

```text
Guangzhou city skyline, Guangzhou aerial city, Guangzhou CBD skyline, Guangzhou Canton Tower
```

## 中国五大城市完整示例

脚本：

```text
第五广州常住人口约1898万人。
面积约7434平方公里。
2024年GDP约3.10万亿元。
千年商都广州把烟火气炼成硬实力。
第四重庆常住人口约3190万人。
面积约8.24万平方公里。
2024年GDP约3.20万亿元。
山城重庆把西部的雄心刻进长江两岸。
第三深圳常住人口约1799万人。
面积约1997平方公里。
2024年GDP约3.68万亿元。
深圳用四十多年把速度跑成世界奇迹。
第二北京常住人口约2183万人。
面积约1.64万平方公里。
2024年GDP约4.98万亿元。
北京不是一座城市的中心而是一个时代的坐标。
第一上海常住人口约2480万人。
面积约6340平方公里。
2024年GDP约5.39万亿元。
黄浦江畔的上海把中国经济高度写进天际线。
```

关键词：

```text
广州城市天际线，广州航拍城市，广州CBD天际线，广州小蛮腰，重庆城市天际线，重庆山城航拍，重庆夜景天际线，重庆长江城市，深圳城市天际线，深圳航拍城市，深圳科技城市天际线，深圳福田天际线，北京城市天际线，北京航拍城市，北京CBD天际线，北京天安门城市，上海城市天际线，上海航拍城市，上海陆家嘴天际线，上海黄浦江天际线
```

命令：

```bash
.venv/bin/python cli.py \
  --task-id china-top-5-cities \
  --video-subject "中国GDP前五大城市排行榜" \
  --video-script-file /tmp/china_cities_script.txt \
  --video-terms-file /tmp/china_cities_terms.txt \
  --video-source pexels \
  --video-aspect 9:16 \
  --video-concat-mode sequential \
  --video-transition-mode none \
  --video-clip-duration 3 \
  --match-materials-to-script \
  --voice-name zh-CN-XiaoxiaoNeural-Female \
  --voice-rate 1.0 \
  --voice-volume 1.0 \
  --bgm-type none \
  --subtitle-enabled \
  --font-name MicrosoftYaHeiBold.ttc \
  --subtitle-position bottom \
  --font-size 60 \
  --stroke-color '#000000' \
  --stroke-width 1.5
```

## Prompt 模板：生成脚本

用于让 LLM 生成视频脚本：

```text
你是短视频口播脚本作者。

主题：{video_subject}
语言：中文
时长：{duration_seconds} 秒
结构：每个主体 3-4 句，每句单独一行。
风格：信息密度高，语气霸气，避免重复主语。
要求：
1. 只输出可以朗读的脚本正文。
2. 不要标题，不要 Markdown，不要解释。
3. 每一句都要适合独立匹配一个画面。
4. 数据句要短，情绪句要有记忆点。
5. 如果是排名视频，每个主体第一句必须强调名次。
```

示例用户 prompt：

```text
生成一个中国五大城市介绍视频脚本。
从第五到第一。
每个城市约 12 秒。
每个城市 4 句。
介绍常住人口、面积、2024 年 GDP，最后一句霸气总结。
不要重复城市名太多。
```

## Prompt 模板：生成逐句关键词

用于让 LLM 根据脚本生成素材搜索关键词：

```text
你是 stock video search keyword generator。

输入脚本：
{video_script}

任务：
1. 按脚本顺序，为每一句生成 1 个视频搜索关键词。
2. 关键词必须是英文。
3. 每个关键词 1-5 个英文词。
4. 如果句子提到城市、地标或行业，要保留具体名称。
5. 输出数量必须等于脚本句子数量。
6. 只输出一行，用英文逗号分隔，不要解释。
```

示例输出：

```text
Guangzhou city skyline, Guangzhou aerial city, Guangzhou CBD skyline, Guangzhou Canton Tower
```

## Prompt 模板：从中文关键词翻译成英文素材搜索词

```text
把下面中文短视频素材关键词转换成英文 stock-video search terms。

要求：
1. 保持原顺序。
2. 每个关键词 1-5 个英文词。
3. 城市名、地标名要准确翻译。
4. 不要解释。
5. 只输出一行，用英文逗号分隔。

中文关键词：
{chinese_terms}
```

## 参数速查

| 参数 | 用途 |
| --- | --- |
| `--task-id` | 指定任务目录名，便于复现 |
| `--video-subject` | 视频主题，必填 |
| `--video-script` | 直接传脚本 |
| `--video-script-file` | 从 UTF-8 文件读取脚本 |
| `--video-terms` | 直接传关键词，支持 `,` 和 `，` |
| `--video-terms-file` | 从 UTF-8 文件读取关键词 |
| `--video-source` | `pexels`、`pixabay`、`coverr` 或 `local` |
| `--video-materials` | 本地素材文件名，只有 `local` 模式需要 |
| `--match-materials-to-script` | 开启逐句字幕素材匹配 |
| `--video-aspect` | 常用 `9:16` 或 `16:9` |
| `--video-transition-mode none` | 关闭转场，方便检查时序 |
| `--voice-name` | TTS voice，中文推荐 `zh-CN-XiaoxiaoNeural-Female` |
| `--bgm-type none` | 关闭 BGM，方便检查口播 |
| `--subtitle-enabled` | 开启字幕 |
| `--font-name MicrosoftYaHeiBold.ttc` | 中文字幕推荐字体 |

## 结果检查

检查文件是否存在：

```bash
ls -lh storage/tasks/<task-id>/final-1.mp4
```

用 ffmpeg 解码验证：

```bash
FF=".venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"
"$FF" -hide_banner -v error -i storage/tasks/<task-id>/final-1.mp4 -f null -
```

抽帧检查字幕和画面：

```bash
mkdir -p /tmp/mpt_frames
"$FF" -hide_banner -y -ss 10 -i storage/tasks/<task-id>/final-1.mp4 -frames:v 1 /tmp/mpt_frames/frame_10s.png
```

检查逐句匹配：

```bash
python -m json.tool storage/tasks/<task-id>/script.json | less
```

重点看：

```json
"matched_segments"
```

## 常见问题

### 中文语音变成英文口音

检查 `--voice-name`。如果传入英文 voice，后端会对中文脚本自动 fallback 到：

```text
zh-CN-XiaoxiaoNeural
```

仍建议显式传：

```bash
--voice-name zh-CN-XiaoxiaoNeural-Female
```

### 字幕和画面不匹配

开启：

```bash
--match-materials-to-script
```

并确保：

```text
脚本句子数量 = 关键词数量
```

### Coverr 找不到中国城市素材

Coverr 对具体中国城市关键词可能返回空结果。具体城市、地标、竖屏短视频优先用：

```bash
--video-source pexels
```

Coverr 更适合：

```text
business meeting
office work
city night
startup team
nature landscape
```

### 本地 LLM 生成关键词太慢

长脚本逐句生成关键词可能很慢。更稳的方式是手动提供 `--video-terms-file`，或者先用 Prompt 模板单独生成关键词，再传给 CLI。
