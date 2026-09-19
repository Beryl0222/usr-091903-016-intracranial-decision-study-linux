"""持久化原语：原子 JSON 存储、只写原始流、哈希链审计账。

所有写入都是追加或原子替换（临时文件 + os.replace），
原始流（raw stream）一经登记只允许追加、禁止修改。
"""

import hashlib
import json
import os
import tempfile
import threading
import time


def utc_now():
    """统一的时间戳来源，单调毫秒精度。"""
    return time.time()


def content_sha256(data):
    """字节内容的 SHA-256（原始流完整性凭证）。"""
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


class JsonStore:
    """以单个 JSON 文件承载命名文档集合，写入原子化。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def collection(self, name):
        with self._lock:
            return self._data.setdefault(name, {})

    def save(self):
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".json-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                os.replace(tmp_path, self.path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise


class RawStreamStore:
    """原始设备流的只写仓库：登记后不可改，仅允许整段追加。

    时钟校正、数据分段都作用在“对齐视图/分段”派生对象上，
    绝不回写这里的原始字节。
    """

    def __init__(self, directory):
        self.directory = directory
        self._lock = threading.RLock()
        os.makedirs(directory, exist_ok=True)

    def _path(self, stream_id):
        return os.path.join(self.directory, f"{stream_id}.bin")

    def register(self, stream_id, label, recorded_at, first_chunk=b""):
        with self._lock:
            path = self._path(stream_id)
            if os.path.exists(path):
                raise ValueError(f"原始流 {stream_id} 已登记，不可覆盖")
            with open(path, "wb") as handle:
                handle.write(first_chunk)
            return self._manifest(stream_id, label, recorded_at)

    def append(self, stream_id, label, recorded_at, chunk):
        with self._lock:
            path = self._path(stream_id)
            if not os.path.exists(path):
                # 首次写入即登记。
                with open(path, "wb") as handle:
                    handle.write(chunk)
            else:
                with open(path, "ab") as handle:
                    handle.write(chunk)
            return self._manifest(stream_id, label, recorded_at)

    def _manifest(self, stream_id, label, recorded_at):
        path = self._path(stream_id)
        with open(path, "rb") as handle:
            payload = handle.read()
        return {
            "stream_id": stream_id,
            "label": label,
            "recorded_at": recorded_at,
            "bytes": len(payload),
            "sha256": content_sha256(payload),
        }

    def digest(self, stream_id):
        with self._lock:
            path = self._path(stream_id)
            if not os.path.exists(path):
                raise ValueError(f"原始流 {stream_id} 不存在")
            with open(path, "rb") as handle:
                return content_sha256(handle.read())

    def prefix_digest(self, stream_id, nbytes):
        """返回前 nbytes 字节的摘要与实际读取长度。

        用于校验“已存在的原始字节未被修改”，同时允许流继续追加。
        """
        with self._lock:
            path = self._path(stream_id)
            if not os.path.exists(path):
                raise ValueError(f"原始流 {stream_id} 不存在")
            with open(path, "rb") as handle:
                prefix = handle.read(nbytes)
            return content_sha256(prefix), len(prefix)


class AuditLog:
    """防篡改的追加账：每条记录引用前一条哈希，可独立校验链。"""

    GENESIS = "GENESIS"

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("")
        self._tail_hash = self.GENESIS
        self._count = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                self._tail_hash = json.loads(line)["entry_hash"]
                self._count += 1

    @staticmethod
    def _hash(record):
        return hashlib.sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def append(self, action, actor, details):
        with self._lock:
            record = {
                "seq": self._count,
                "ts": utc_now(),
                "actor": actor,
                "action": action,
                "details": details,
                "prev_hash": self._tail_hash,
            }
            record["entry_hash"] = self._hash(
                {k: v for k, v in record.items() if k != "entry_hash"}
            )
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._tail_hash = record["entry_hash"]
            self._count += 1
            return record

    def count(self):
        with self._lock:
            return self._count

    def verify_chain(self):
        """重放整条链，返回 (是否完整, 首个断裂序号或 None)。"""
        with self._lock:
            prev = self.GENESIS
            seq = 0
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    stored = record.pop("entry_hash")
                    if record["prev_hash"] != prev:
                        return False, record["seq"]
                    if self._hash(record) != stored:
                        return False, record["seq"]
                    prev = stored
                    record["entry_hash"] = stored
                    if record["seq"] != seq:
                        return False, seq
                    seq += 1
            return True, None
