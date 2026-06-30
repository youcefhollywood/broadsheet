# v: bsh-sts-03
"""
Broadsheet — state store (OSS via plain REST, no oss2).

We talk to OSS directly over its REST API using `requests` + manual request
signing. This deliberately avoids the oss2 / aliyunsdkcore / pyOpenSSL import
chain, which is broken in the FC custom-runtime base image (cryptography vs
pyOpenSSL version clash). For a single get/put of one JSON object, the REST API
is a few lines and has no heavy dependencies.

Auth: temporary RAM-role credentials are exposed by FC as the ALIBABA_CLOUD_*
env vars (access key id / secret / security token).

State lives at:  oss://<your-bucket>/state/broadsheet_state.json
"""

import os
import json
import hmac
import base64
import hashlib
import datetime

import requests

BUCKET    = os.getenv("OSS_BUCKET", "your-bucket-name")
# OSS endpoint host. Use the internal endpoint when the function and bucket are in
# the same region (free + fast); the public endpoint works from anywhere.
ENDPOINT_HOST = os.getenv("OSS_ENDPOINT", "oss-<region>-internal.aliyuncs.com")
STATE_KEY = os.getenv("OSS_STATE_KEY", "state/broadsheet_state.json")
MAX_EDITIONS_KEPT = 6


def _empty_state():
    from preference import new_memory
    return {
        "memory": new_memory(),
        "world_state": "",
        "editions": [],
        "pending_question": None,
    }


def _creds():
    return (
        os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID"),
        os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
        os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN"),
    )


def _gmt_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _sign(method, key, date, content_type="", security_token=""):
    """
    Build the OSS Authorization header (signature V1).
    StringToSign = METHOD\n CONTENT-MD5\n CONTENT-TYPE\n DATE\n CanonicalizedOSSHeaders + CanonicalizedResource
    """
    ak, sk, token = _creds()
    canon_headers = ""
    if security_token:
        canon_headers = "x-oss-security-token:" + security_token + "\n"
    canon_resource = "/" + BUCKET + "/" + key
    string_to_sign = method + "\n\n" + content_type + "\n" + date + "\n" + canon_headers + canon_resource
    signature = base64.b64encode(
        hmac.new(sk.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    return "OSS " + ak + ":" + signature


def _url(key):
    return "https://" + BUCKET + "." + ENDPOINT_HOST + "/" + key


def load_state():
    """GET the state object from OSS, or a fresh empty one if it doesn't exist."""
    ak, sk, token = _creds()
    date = _gmt_now()
    headers = {"Date": date, "Authorization": _sign("GET", STATE_KEY, date, "", token)}
    if token:
        headers["x-oss-security-token"] = token
    try:
        r = requests.get(_url(STATE_KEY), headers=headers, timeout=20)
        if r.status_code == 404:
            return _empty_state()
        r.raise_for_status()
        return json.loads(r.content.decode("utf-8"))
    except Exception:
        return _empty_state()


def save_state(state):
    """PUT the state object to OSS as JSON."""
    state["editions"] = state.get("editions", [])[:MAX_EDITIONS_KEPT]
    body = json.dumps(state, ensure_ascii=False).encode("utf-8")
    ak, sk, token = _creds()
    date = _gmt_now()
    ctype = "application/json"
    headers = {
        "Date": date,
        "Content-Type": ctype,
        "Authorization": _sign("PUT", STATE_KEY, date, ctype, token),
    }
    if token:
        headers["x-oss-security-token"] = token
    r = requests.put(_url(STATE_KEY), data=body, headers=headers, timeout=20)
    r.raise_for_status()
