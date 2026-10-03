"""加载CTA回测的翻译文本。"""

from pathlib import Path
import gettext

localedir = Path(__file__).parent
translations = gettext.translation('vnpy_ctabacktester', localedir=localedir, fallback=True)

_ = translations.gettext
