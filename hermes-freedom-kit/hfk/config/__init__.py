"""配置读写子包：键路径语义 + 保注释 YAML 写入。"""

from hfk.config.keypath import get_nested, has_nested, join_key_path, split_key_path
from hfk.config.yaml_io import load_document, read_text, update_key

__all__ = [
    "get_nested",
    "has_nested",
    "join_key_path",
    "split_key_path",
    "load_document",
    "read_text",
    "update_key",
]
