"""Format outbound API requests and responses for the debug log."""

import json
import re
import urllib.parse


MAX_MESSAGE = 12000
MAX_REQUEST_PREVIEW = 2000
MASK = "[已隐藏]"
SENSITIVE_KEYS = {
    "apikey", "xapikey", "azurekey", "ocpapimsubscriptionkey", "authorization", "password", "passwd", "passkey", "secret", "clientsecret",
    "cookie", "setcookie", "sid", "sessionid", "sessiontoken", "token", "accesstoken", "refreshtoken",
    "credential", "credentials", "authtoken", "dltoken", "downloadtoken", "downloadurl", "downloadlink",
    "tracker", "trackers", "trackerurl", "announce", "announcelist", "magnet", "magneturi",
}
SENSITIVE_PAYLOADS = {"/torrent/gendltoken", "/subtitle/genlink", "/subtitle/dlv2", "/torrent/download"}
URL_PATTERN = re.compile(r"(?:https?|udp)://[^\s<>\"'\\]+", re.IGNORECASE)
REQUEST_SENSITIVE_KEYS = {"apitoken", "authkey", "proxyauthorization", "username", "urls", "torrents"}


def scrub_url(match):
    try:
        url = urllib.parse.urlsplit(match[0])
    except ValueError:
        return MASK
    if re.search(r"/(?:announce|tracker|download)(?:/|$)", url.path, re.IGNORECASE):
        return url._replace(netloc=url.netloc.rsplit("@", 1)[-1], path="/" + MASK, query="", fragment="").geturl()
    return url._replace(netloc=url.netloc.rsplit("@", 1)[-1],
                        query=MASK if url.query else "", fragment=MASK if url.fragment else "").geturl()


def sanitized_data(data, endpoint, secrets, *, request=False):
    endpoint = endpoint.casefold()
    hidden_values = {value for value in secrets if isinstance(value, str) and value}
    for value in tuple(hidden_values):
        hidden_values.update((urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)))
    hidden_values = sorted(hidden_values, key=len, reverse=True)

    def clean_text(value):
        for secret in hidden_values:
            if len(secret) < 4:
                value = re.sub(r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])", MASK, value)
            else:
                value = value.replace(secret, MASK)
        return URL_PATTERN.sub(scrub_url, value)

    def scrub(value, depth=0):
        if depth > 20:
            return "[已省略过深的数据]"
        if isinstance(value, dict):
            fields = {}
            for key, item in value.items():
                field = re.sub(r"[^a-z0-9]", "", key.casefold())
                hide = (field in SENSITIVE_KEYS or (request and field in REQUEST_SENSITIVE_KEYS)
                        or (endpoint in SENSITIVE_PAYLOADS and field in {"data", "url", "link"})
                        or (endpoint.endswith("/trackers") and field == "url"))
                fields[clean_text(key)] = MASK if hide else scrub(item, depth + 1)
            return fields
        if isinstance(value, list):
            return [scrub(item, depth + 1) for item in value]
        return clean_text(value) if isinstance(value, str) else value

    return scrub(data)


def query_parameters(query):
    return {
        key: values[0] if len(values) == 1 else values
        for key, values in urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=256).items()
    }


def request_preview(endpoint, query, payload, secrets):
    query_params = query_parameters(query)
    if not (query_params or payload):
        return ""
    data = {"query": query_params, "body": payload} if query_params and payload else (query_params or payload)
    preview = json.dumps(sanitized_data(data, endpoint, secrets, request=True), ensure_ascii=False, separators=(", ", ": "))
    if len(preview) > MAX_REQUEST_PREVIEW:
        return preview[:MAX_REQUEST_PREVIEW] + "…（请求参数过长，已截断）"
    return preview


def response_preview(raw, endpoint, secrets, normalize_code, code=""):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    if not raw or not raw.lstrip().startswith(("{", "[")):
        return None
    return response_data_preview(json.loads(raw), endpoint, secrets, normalize_code, code)


def response_data_preview(data, endpoint, secrets, normalize_code, code=""):
    sanitized = sanitized_data(data, endpoint, secrets)
    if not code:
        record = sanitized
        for _ in range(4):
            if isinstance(record, list) and len(record) == 1:
                record = record[0]
            elif not isinstance(record, dict):
                break
            else:
                for field in ("code", "name", "title", "productNumber"):
                    code = normalize_code(record.get(field))
                    if not code and field == "productNumber":
                        product = re.fullmatch(r"([a-z]+)([0-9]+)", str(record.get(field) or ""), re.IGNORECASE)
                        if product:
                            code = f"{product[1].upper()}-{product[2]}"
                    if code:
                        break
                if code:
                    break
                record = next((record[key] for key in ("data", "torrent", "detail", "item") if isinstance(record.get(key), (list, dict))), None)
    return code, json.dumps(sanitized, ensure_ascii=False, indent=2)
