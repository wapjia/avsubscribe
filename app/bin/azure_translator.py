"""Translate Japanese DMM metadata with Azure AI Translator."""

import http.client
import json
import re
import urllib.error
import urllib.request


TRANSLATE_URL = "https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=ja&to=zh-Hans"
BASE_INFO_LABELS = {
    "ジャンル": "类型", "シリーズ": "系列", "品番": "番号", "平均評価": "平均评分",
    "収録時間": "时长", "レーベル": "厂牌", "発売日": "发行日期", "出演者": "演员",
    "メーカー": "制作商", "監督": "导演", "関連タグ": "相关标签",
}
HASHTAG = re.compile(r"([#＃])([^\s#＃,，、;；<>()（）]+(?:[ \u3000]+[^\s#＃,，、;；<>()（）]+)*)")
HASHTAG_TRANSLATION_VERSION = 1


class TranslationError(Exception):
    def __init__(self, message, unavailable=False):
        super().__init__(message)
        self.unavailable = unavailable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def contains_hashtags(value):
    if isinstance(value, str):
        return value.lstrip().startswith(("#", "＃")) and HASHTAG.search(value) is not None
    if isinstance(value, dict):
        return any(contains_hashtags(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_hashtags(item) for item in value)
    return False


def translate_batch(texts, key, region, on_response=None):
    headers = {"Content-Type": "application/json; charset=UTF-8", "Ocp-Apim-Subscription-Key": key}
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    body = json.dumps([{"Text": text} for text in texts], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(TRANSLATE_URL, data=body, headers=headers, method="POST")
    try:
        with OPENER.open(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if on_response:
                on_response(response.status, raw)
            result = json.loads(raw)
        if not isinstance(result, list) or len(result) != len(texts):
            raise ValueError
        translations = [item["translations"][0]["text"] for item in result]
        if not all(isinstance(text, str) for text in translations):
            raise ValueError
        return [text if text.strip() else original for original, text in zip(texts, translations)]
    except urllib.error.HTTPError as error:
        with error:
            if on_response:
                try:
                    on_response(error.code, error.read(2 * 1024 * 1024 + 1))
                except (OSError, http.client.HTTPException):
                    pass
            raise TranslationError(f"Azure 翻译请求失败（HTTP {error.code}）。", error.code in (401, 403, 429)) from None
    except (OSError, http.client.HTTPException):
        raise TranslationError("无法连接 Azure 翻译接口。", unavailable=True) from None
    except (IndexError, KeyError, TypeError, ValueError):
        raise TranslationError("Azure 翻译响应格式无效。") from None


def translate_metadata(fields, key, region, on_response=None):
    inputs = {}

    def collect_text(text):
        if re.search(r"[ぁ-ヿ一-龯々]", text):
            inputs.setdefault(text, text)

    def collect(value):
        if isinstance(value, str):
            if contains_hashtags(value):
                for match in HASHTAG.finditer(value):
                    collect_text(match[2])
            else:
                collect_text(value)
        elif isinstance(value, dict):
            for label, item in value.items():
                if label not in BASE_INFO_LABELS:
                    collect(label)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    def apply(value):
        if isinstance(value, dict):
            return {BASE_INFO_LABELS.get(label, inputs.get(label, label)): apply(item) for label, item in value.items()}
        if isinstance(value, list):
            return [apply(item) for item in value]
        if isinstance(value, str) and contains_hashtags(value):
            return HASHTAG.sub(lambda match: match[1] + inputs.get(match[2], match[2]), value)
        return inputs.get(value, value)

    for value in fields.values():
        collect(value)
    batch = []
    size = 0
    for text in inputs:
        length = len(text)
        if length > 50000:
            raise TranslationError("单项 DMM 文本超过 Azure 翻译长度限制。")
        if len(batch) == 100 or size + length > 50000:
            inputs.update(zip(batch, translate_batch(batch, key, region, on_response)))
            batch, size = [], 0
        batch.append(text)
        size += length
    if batch:
        inputs.update(zip(batch, translate_batch(batch, key, region, on_response)))
    return {field: apply(value) for field, value in fields.items()}
