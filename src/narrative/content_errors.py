"""Typed business validation failures; transport/configuration errors are separate."""
import json
from contextlib import contextmanager
from functools import wraps

from pydantic import ValidationError


class ContentValidationError(ValueError):
    """The submitted authored fields may be staged for a local repair."""
    def __init__(self, message, *, problems=None):
        super().__init__(message)
        self.problems = problems or []


class ContentConflictError(ValueError):
    """The captured business revision or dependency snapshot is no longer current."""


class ContentGuardError(ContentConflictError):
    """The requested target/action is outside the authorized current scope."""


@contextmanager
def content_validation():
    """Classify only deterministic ValueError checks inside a content boundary."""
    try:
        yield
    except (ContentValidationError, ContentConflictError, ValidationError, json.JSONDecodeError, UnicodeError):
        raise
    except ValueError as error:
        raise ContentValidationError(str(error)) from error


def content_write(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        with content_validation():
            return function(*args, **kwargs)
    return invoke


def content_guard(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ContentConflictError, json.JSONDecodeError, UnicodeError):
            raise
        except ValueError as error:
            raise ContentGuardError(str(error)) from error
    return invoke


def _located_errors(error, prefix, value, payload_fields=None):
    result = []
    for item in error.errors(include_url=False, include_context=True, include_input=False):
        cause = (item.get('ctx') or {}).get('error')
        if isinstance(cause, ContentValidationError) and cause.problems:
            for problem in cause.problems:
                row = dict(problem, path=[*prefix, *item['loc'], *problem.get('path', [])])
                row.setdefault('model_location', list(item['loc']))
                if 'related_paths' in problem:
                    row['related_paths'] = [[*prefix, *item['loc'], *path] for path in problem['related_paths']]
                result.append(row)
            continue
        current, path = value, list(prefix)
        for component in item['loc']:
            if isinstance(current, dict) and component in (current.get('op'), current.get('type')) and component not in current:
                continue  # Pydantic discriminated-union labels are not JSON keys.
            if (isinstance(current, dict) and payload_fields and isinstance(current.get('type'), str)
                    and component not in current and isinstance(current.get('payload'), dict)
                    and (component in payload_fields.get(current['type'], ()) or component in current['payload'])):
                path.append('payload')
                current = current['payload']
            path.append(component)
            if isinstance(current, dict):
                current = current.get(component)
            elif isinstance(current, list) and isinstance(component, int) and 0 <= component < len(current):
                current = current[component]
            else:
                current = None
        result.append({'code': 'CONTENT_VALIDATION', 'message': item['msg'], 'path': path,
            'model_location': list(item['loc'])})
    return result


def validate_content(adapter, value, argument, *, payload_fields=None):
    try:
        return adapter.validate_python(value)
    except ValidationError as error:
        raise ContentValidationError(str(error), problems=_located_errors(error, [argument], value, payload_fields)) from error


@contextmanager
def content_item(path, value=None):
    """Locate a check performed while processing this actual input item only."""
    try:
        yield
    except (ContentConflictError, json.JSONDecodeError, UnicodeError):
        raise
    except ContentValidationError as error:
        if not error.problems:
            error.problems = [{'code': 'CONTENT_VALIDATION', 'message': str(error), 'path': list(path)}]
        raise
    except ValidationError as error:
        raise ContentValidationError(str(error), problems=_located_errors(error, path, value)) from error
    except ValueError as error:
        raise ContentValidationError(str(error), problems=[{'code': 'CONTENT_VALIDATION', 'message': str(error), 'path': list(path)}]) from error
