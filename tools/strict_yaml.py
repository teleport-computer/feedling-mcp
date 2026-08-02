"""Strict YAML loading helpers for deployment configuration."""

from io import StringIO
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _StrictSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {node.id}",
                node.start_mark,
            )

        self.flatten_mapping(node)
        mapping = {}
        key_marks = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc

            if duplicate:
                raise ConstructorError(
                    f"first definition of mapping key {key!r}",
                    key_marks[key],
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )

            mapping[key] = self.construct_object(value_node, deep=deep)
            key_marks[key] = key_node.start_mark
        return mapping


def load_yaml_strict(
    source: str,
    *,
    source_name: str = "<string>",
) -> Any:
    """Load YAML safely and reject duplicate keys in every mapping."""

    stream = StringIO(source)
    stream.name = source_name
    return yaml.load(stream, Loader=_StrictSafeLoader)
