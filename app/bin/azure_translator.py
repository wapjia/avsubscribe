"""Translate Japanese DMM metadata with Azure AI Translator."""

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter


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


def actress_name_pattern(names):
    return re.compile("|".join(map(re.escape, sorted(names, key=len, reverse=True)))) if names else None


def contains_actress_name(value, pattern):
    if isinstance(value, str):
        return pattern.search(value) is not None
    if isinstance(value, dict):
        return any(pattern.search(label) or contains_actress_name(item, pattern) for label, item in value.items())
    if isinstance(value, list):
        return any(contains_actress_name(item, pattern) for item in value)
    return False


def translate_batch(texts, key, region, on_response=None):
    headers = {"Content-Type": "application/json; charset=UTF-8", "Ocp-Apim-Subscription-Key": key}
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    payload = [{"Text": text} for text in texts]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(TRANSLATE_URL, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with OPENER.open(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if on_response:
                on_response(response.status, raw, time.perf_counter() - started, payload)
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
                    raw = error.read(2 * 1024 * 1024 + 1)
                    on_response(error.code, raw, time.perf_counter() - started, payload)
                except (OSError, http.client.HTTPException):
                    pass
            raise TranslationError(f"Azure 翻译请求失败（HTTP {error.code}）。", error.code in (401, 403, 429)) from None
    except (OSError, http.client.HTTPException):
        raise TranslationError("无法连接 Azure 翻译接口。", unavailable=True) from None
    except (IndexError, KeyError, TypeError, ValueError):
        raise TranslationError("Azure 翻译响应格式无效。") from None


def translate_metadata(fields, key, region, on_response=None, actress_translations=None):
    replacements = {
        name: translation.strip()
        for name, translation in (actress_translations or {}).items()
        if isinstance(name, str) and name and isinstance(translation, str) and translation.strip()
    }
    placeholders = {
        name: f"__AVSUB_ACTRESS_{index:04d}__"
        for index, name in enumerate(replacements)
    }
    name_pattern = actress_name_pattern(placeholders)
    restore_placeholders = {placeholder: replacements[name] for name, placeholder in placeholders.items()}
    placeholder_pattern = actress_name_pattern(restore_placeholders)

    def protect(value):
        if isinstance(value, str):
            return name_pattern.sub(lambda match: placeholders[match.group()], value) if name_pattern else value
        if isinstance(value, dict):
            return {protect(label): protect(item) for label, item in value.items()}
        if isinstance(value, list):
            return [protect(item) for item in value]
        return value

    def restore(value):
        if isinstance(value, str):
            if placeholder_pattern:
                return placeholder_pattern.sub(lambda match: restore_placeholders[match.group()], value)
            return value
        if isinstance(value, dict):
            return {restore(label): restore(item) for label, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        return value

    protected_fields = protect(fields)
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

    for value in protected_fields.values():
        collect(value)
    def translate_inputs(values):
        batch = []
        size = 0
        for text in values:
            length = len(text)
            if length > 50000:
                raise TranslationError("单项 DMM 文本超过 Azure 翻译长度限制。")
            if len(batch) == 100 or size + length > 50000:
                values.update(zip(batch, translate_batch(batch, key, region, on_response)))
                batch, size = [], 0
            batch.append(text)
            size += length
        if batch:
            values.update(zip(batch, translate_batch(batch, key, region, on_response)))

    translate_inputs(inputs)
    if placeholder_pattern:
        parts_pattern = re.compile(f"({placeholder_pattern.pattern})")
        retry_parts = {}
        fragments = {}
        for original, translated in inputs.items():
            if Counter(placeholder_pattern.findall(original)) != Counter(placeholder_pattern.findall(translated)):
                parts = parts_pattern.split(original)
                retry_parts[original] = parts
                for part in parts[::2]:
                    if re.search(r"[ぁ-ヿ一-龯々]", part):
                        fragments.setdefault(part, part)
        translate_inputs(fragments)
        for original, parts in retry_parts.items():
            parts[::2] = [fragments.get(part, part) for part in parts[::2]]
            inputs[original] = "".join(parts)
    return restore({field: apply(value) for field, value in protected_fields.items()})
