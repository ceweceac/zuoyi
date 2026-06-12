"""富文本 HTML → 图片渲染：把群发文案编辑器产出的富文本（带颜色/字号/加粗）
渲染成一张 PNG 图片，用于钉钉群发。

为什么发图片：钉钉自定义机器人 markdown 不支持字体颜色和字号。要在群里呈现
带颜色/字号的排版，唯一可行方式是渲染成图片再发。

技术：html2image（驱动本机 Chrome 内核截图），中文字体用系统 PingFang/STHeiti。
渲染出的 PNG 走 uploader.save_upload 落盘 + 拿到可访问 URL（与普通上传图同路径）。
"""
import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# 包裹用户富文本的页面模板：固定宽度、白底、内边距、中文字体兜底。
# {body} 是 ui.editor 产出的 HTML 片段。
_PAGE_TPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    width:{width}px;
    font-family:"PingFang SC","STHeiti","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    background:#ffffff;
    padding:32px;
    color:#222;
    line-height:1.7;
    font-size:17px;
    word-wrap:break-word;
  }}
  h1 {{ font-size:26px; margin:10px 0; }}
  h2 {{ font-size:22px; margin:9px 0; }}
  h3 {{ font-size:19px; margin:8px 0; }}
  p {{ margin:7px 0; }}
  ul,ol {{ margin:7px 0 7px 26px; }}
  img {{ max-width:100%; }}
  a {{ color:#2a7ae2; }}
  blockquote {{ border-left:4px solid #ddd; padding-left:12px; color:#666; margin:8px 0; }}
</style></head><body>{body}</body></html>"""


def render_html_to_png(body_html: str, width: int = 640) -> bytes:
    """把富文本 HTML 片段渲染成 PNG 字节。失败抛异常。"""
    if not body_html or not body_html.strip():
        raise ValueError("内容为空，无法渲染")
    if chr(0xfffd) in body_html:
        raise ValueError("内容含损坏字符（U+FFFD），已拒绝渲染")

    from html2image import Html2Image
    page = _PAGE_TPL.format(width=width, body=body_html)
    # 高度给足，截图后浏览器按内容自适应；size 只约束宽度，高度设大值避免裁切
    with tempfile.TemporaryDirectory() as tmpd:
        hti = Html2Image(output_path=tmpd, size=(width, 4000),
                         custom_flags=["--no-sandbox", "--hide-scrollbars",
                                       "--default-background-color=FFFFFFFF",
                                       "--disable-gpu"])
        out_name = "broadcast_render.png"
        hti.screenshot(html_str=page, save_as=out_name)
        out_path = Path(tmpd) / out_name
        if not out_path.exists():
            raise RuntimeError("渲染失败：未生成图片")
        data = out_path.read_bytes()
    # 自适应裁掉底部多余白边
    try:
        data = _trim_bottom_whitespace(data)
    except Exception as e:
        log.warning("trim whitespace failed (ignored): %s", e)
    return data


def _trim_bottom_whitespace(png_bytes: bytes) -> bytes:
    """裁掉底部纯白区域（固定 4000 高会留大片空白）。用 Pillow。"""
    import io
    from PIL import Image, ImageChops
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg)
    bbox = diff.getbbox()  # (left, upper, right, lower) 非白区域
    if bbox:
        # 保留左右满宽，只按内容裁高度，底部留 24px 边距
        lower = min(im.height, bbox[3] + 24)
        im = im.crop((0, 0, im.width, lower))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def render_and_save(body_html: str, uploaded_by: str = "admin", width: int = 640) -> dict:
    """渲染富文本为图片并通过 uploader 落盘入库。
    返回 uploader.save_upload 的结果 dict（含 public_url / id / dingtalk_media_id 等）。
    """
    png = render_html_to_png(body_html, width=width)
    from . import uploader
    import time
    return uploader.save_upload(png, f"broadcast_{int(time.time())}.png", uploaded_by)


# ============================================================
# HTML → 钉钉 markdown 文字 转换
# ui.editor(Quasar QEditor)产出 HTML，钉钉文字消息只认 markdown 子集。
# 颜色/字号(span style)在转换时丢弃(钉钉文字消息不支持)，保留结构语义。
# ============================================================
import re as _re
from html import unescape as _unescape


def html_to_dingtalk_markdown(html: str) -> str:
    """把富文本编辑器的 HTML 转成钉钉 markdown 文字。

    支持：标题 h1-h6、加粗 b/strong、斜体 i/em、删除线 s/del、
    列表 ul/ol+li、链接 a、引用 blockquote、换行 br、段落 div/p。
    丢弃：颜色/字号等 style（钉钉文字消息不支持）。
    """
    if not html:
        return ""
    s = html
    # 块级换行标记：先把 </p></div></h*></li> 等转成换行锚点
    s = _re.sub(r"(?i)<br\s*/?>", "\n", s)
    # 标题：<h1>x</h1> -> \n# x\n
    for level in range(1, 7):
        s = _re.sub(r"(?is)<h%d[^>]*>(.*?)</h%d>" % (level, level),
                    lambda m, lv=level: "\n" + "#" * lv + " " + m.group(1).strip() + "\n", s)
    # 加粗 / 斜体 / 删除线
    s = _re.sub(r"(?is)<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>", lambda m: "**" + m.group(1).strip() + "**", s)
    s = _re.sub(r"(?is)<(?:i|em)[^>]*>(.*?)</(?:i|em)>", lambda m: "*" + m.group(1).strip() + "*", s)
    s = _re.sub(r"(?is)<(?:s|del|strike)[^>]*>(.*?)</(?:s|del|strike)>", lambda m: "~~" + m.group(1).strip() + "~~", s)
    # 链接 <a href="x">y</a> -> [y](x)
    s = _re.sub(r'(?is)<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
                lambda m: "[%s](%s)" % (m.group(2).strip(), m.group(1).strip()), s)
    # 引用
    s = _re.sub(r"(?is)<blockquote[^>]*>(.*?)</blockquote>",
                lambda m: "\n> " + m.group(1).strip() + "\n", s)
    # 有序列表
    def _ol(m):
        items = _re.findall(r"(?is)<li[^>]*>(.*?)</li>", m.group(1))
        return "\n" + "\n".join("%d. %s" % (i + 1, _strip_tags(it).strip()) for i, it in enumerate(items)) + "\n"
    s = _re.sub(r"(?is)<ol[^>]*>(.*?)</ol>", _ol, s)
    # 无序列表
    def _ul(m):
        items = _re.findall(r"(?is)<li[^>]*>(.*?)</li>", m.group(1))
        return "\n" + "\n".join("- " + _strip_tags(it).strip() for it in items) + "\n"
    s = _re.sub(r"(?is)<ul[^>]*>(.*?)</ul>", _ul, s)
    # 段落 / div 收尾加换行
    s = _re.sub(r"(?is)</(?:p|div)>", "\n", s)
    s = _re.sub(r"(?is)<(?:p|div)[^>]*>", "", s)
    # 去掉剩余所有标签
    s = _strip_tags(s)
    # 实体反转义
    s = _unescape(s)
    # 收敛多余空行（最多保留一个空行）
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_tags(s: str) -> str:
    return _re.sub(r"(?is)<[^>]+>", "", s)
