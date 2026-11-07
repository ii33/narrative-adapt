"""One real simple-color panel image call through Operations; no image fallback.

The external observation budget is QA-only. Instrumentation copies the exact
provider inputs and transparently calls the original service implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from accept_storyboard_assets import all_assets, screenplay_content
from accept_storyboard_assets import seed as base_seed

PROMPT = '近景表现林岚手掌握住折叠伞柄，手指和伞柄接触明确；窗侧柔光照亮手背，接触暗部柔和，不添加硬影。简彩仅保留雨衣灰蓝与伞的黑色，其他颜色未知不猜。'
STYLE_NOTE = '本工程测试约定：窗侧柔光保持柔软接触暗部，简彩少量用于已明确的灰蓝雨衣和黑伞，不改成精细插画。'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def assess_prompt(style, actual_prompt, user_prompt):
    return {
        'effective_style_full_text_forwarded': bool(style) and style in actual_prompt,
        'unified_descriptive_style_header': '统一草图呈现规范' in actual_prompt,
        'simple_color_not_forced_black_and_white': '本次简彩' in actual_prompt and '本次使用黑白灰素描' not in actual_prompt,
        'specific_panel_request_forwarded': user_prompt in actual_prompt,
        'hand_contact_and_soft_light_requested': '手' in user_prompt and '接触' in user_prompt and '柔光' in user_prompt,
        'no_geometry_fallback_claimed': '几何' not in user_prompt,
    }


def assess_receipt(before_board, after_board, fixture, calls, receipt):
    from PIL import Image
    shots = {shot['id']: shot for shot in after_board['shots']}
    panel = next((panel for panel in shots.get(fixture['shot_id'], {}).get('panels', []) if panel['id'] == fixture['panel_id']), {})
    candidates = panel.get('candidates', [])
    image = Path(candidates[0].get('path', '')) if len(candidates) == 1 else Path('')
    image_ok = False
    if image.is_file():
        with Image.open(image) as picture:
            picture.load()
            image_ok = picture.width > 0 and picture.height > 0 and picture.getextrema() != ((255, 255),) * len(picture.getbands())
    basis = candidates[0].get('generation_basis', {}) if candidates else {}
    prompt = candidates[0].get('generation_prompt', '') if candidates else ''
    return {
        'one_actual_provider_function_call': len(calls) == 1,
        'provider_receipt_recorded': isinstance(receipt, dict) and receipt.get('provider') == 'ComfyUI',
        'one_real_generated_candidate': len(candidates) == 1 and image_ok,
        'candidate_retains_simple_color': len(candidates) == 1 and candidates[0].get('simple_color') is True,
        'candidate_keeps_effective_style': basis.get('sketch_style') == fixture['effective_style'],
        'candidate_keeps_actual_generation_prompt': len(calls) == 1 and prompt == calls[0]['prompt'],
        'provider_output_not_internal_guide': image_ok and bool(calls) and image.read_bytes() != Path(calls[0]['saved_guide']).read_bytes(),
        'not_automatically_adopted_or_approved': after_board.get('approved') is False and panel.get('adopted_candidate_id') is None,
        'other_shot_unchanged': all(shots.get(shot['id']) == shot for shot in before_board['shots'] if shot['id'] != fixture['shot_id']),
    }


def provider_configuration():
    from narrative.storyboard_style_provider import _DEFAULT
    raw_service = os.environ.get('NARRATIVE_COMFY_URL') or 'http://127.0.0.1:8188'
    parsed = urlsplit(raw_service)
    workflow = Path(os.environ.get('NARRATIVE_COMFY_SKETCH_WORKFLOW') or os.environ.get('NARRATIVE_COMFY_WORKFLOW') or _DEFAULT).resolve()
    return {'provider': 'ComfyUI', 'service': {'scheme': parsed.scheme, 'host': parsed.hostname,
        'port': parsed.port or 80, 'path': parsed.path, 'credentials_present': bool(parsed.username or parsed.password)},
        'workflow_file': str(workflow), 'workflow_sha256': digest(workflow.read_bytes()) if workflow.is_file() else None,
        'usage': None, 'usage_note': 'Local image service provides no token or billing usage; no LLM request is made.'}


def main():
    from narrative import storyboard_style_provider as provider
    from narrative.operations import Operations
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60:
        raise ValueError('External image QA observation budget must be 1–60 seconds')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['NARRATIVE_PROVIDER_DB'] = str(output/'providers.sqlite3')
    def save(name, value):
        (output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    root = Path(__file__).resolve().parents[1]
    paths = [*root.glob('src/narrative/storyboard*.py'), root/'src/narrative/prompt_defaults.py',
        root/'src/narrative/prompt_settings.py', root/'src/narrative/asset_operations.py',
        root/'src/narrative/screenplay_tools.py', Path(__file__).resolve()]
    report = {'status': 'running', 'qa_version': 'unified-sketch-style-simple-color-panel/1',
        'started': datetime.now(UTC).isoformat(), 'new_model_requests': 0, 'image_operation_calls': 0,
        'provider_function_calls': [], 'http_requests': [], 'automatic_retry': False,
        'external_observation_seconds': args.seconds, 'product_deadline': None,
        'implementation_sha256': {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in paths if path.is_file()},
        'configuration': provider_configuration(), 'user_projects_modified': False,
        'fixture_origin': 'Controlled synthetic scene and blank panel in a new isolated project. Only final image is requested from the real configured service; internal guide is not model output.'}
    save('manifest.json', report)
    ops = None
    fixture = None
    original_render = provider.render_sketch_image
    original_request = provider.ComfyClient.request
    settled = threading.Event()
    began = time.monotonic()
    receipt = None
    before = None
    try:
        ops = Operations.create_graph_project(output/'统一素描简彩画格验收.nproj')
        fixture = base_seed(ops)
        unit = fixture['unit_id']
        board = ops.read_storyboard(unit)
        shot_id = fixture['selected_shot_ids'][0]
        board = ops.apply_storyboard_changes(unit, board['revision'], [
            {'op': 'update_shot', 'shot_id': shot_id, 'title': '窗边收伞近景',
             'description': '林岚在档案室门口的窗边收伞，手指握住伞柄，柔软侧光照在手背。', 'shot_language': '手与伞柄接触近景，固定机位，窗侧柔光'},
            {'op': 'create_panel', 'shot_id': shot_id, 'intent': '仅表现手握住伞柄的接触瞬间，窗侧柔光，必要少量简彩', 'offset_ms': 0}])
        board = ops.read_storyboard(unit)
        panel_id = next(shot for shot in board['shots'] if shot['id'] == shot_id)['panels'][0]['id']
        settings = ops.load_prompt_settings()
        style = next(row['effective'] for row in settings['prompts'] if row['id'] == 'sketch_style') + '\n' + STYLE_NOTE
        ops.save_prompt_override('sketch_style', style, settings['revision'], '隔离统一素描风格简彩验证')
        style_row = next(row for row in ops.load_prompt_settings()['prompts'] if row['id'] == 'sketch_style')
        fixture = fixture | {'shot_id': shot_id, 'panel_id': panel_id, 'effective_style': style_row['effective'],
            'style_version': style_row['version'], 'simple_color': True, 'user_prompt': PROMPT}
        context = ops.read_script_unit_context(unit)
        basis, references = ops._storyboard_sketch_basis(board, next(shot for shot in board['shots'] if shot['id'] == shot_id), 0, context)
        before = {'board': board, 'screenplay': screenplay_content(ops.read_screenplay(fixture['screenplay_id'])),
            'graph': ops.graph_snapshot(), 'adaptation': ops.adaptation_snapshot(), 'assets': all_assets(ops)}
        report['source_sha256'] = digest(json.dumps(before, ensure_ascii=False, sort_keys=True).encode())
        report['basis_sha256'] = digest(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode())
        report['effective_style_sha256'] = digest(fixture['effective_style'].encode())
        report['fixture'] = fixture
        save('source-context.json', context)
        save('generation-basis.json', basis)
        save('fixture.json', fixture)
        (output/'effective-style.txt').write_text(fixture['effective_style'], encoding='utf-8')
        (output/'user-prompt.txt').write_text(PROMPT, encoding='utf-8')
        def observed_render(guide, prompt, copies_to, reference_images=()):
            nonlocal receipt
            number = len(report['provider_function_calls']) + 1
            saved_guide = output/f'actual-guide-{number}.png'
            shutil.copyfile(guide, saved_guide)
            row = {'number': number, 'prompt': prompt, 'guide_sha256': digest(Path(guide).read_bytes()),
                'saved_guide': str(saved_guide), 'reference_images': [{'path': str(path), 'sha256': digest(Path(path).read_bytes())} for path in reference_images]}
            report['provider_function_calls'].append(row)
            (output/f'actual-prompt-{number}.txt').write_text(prompt, encoding='utf-8')
            save('provider-inputs.json', report['provider_function_calls'])
            try:
                receipt = original_render(guide, prompt, copies_to, reference_images=reference_images)
                save('provider-receipt.json', receipt)
                return receipt
            except Exception as error:
                row.update(error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
                save('provider-inputs.json', report['provider_function_calls'])
                raise
        def observed_request(client, method, route, body=None, content_type=None):
            number = len(report['http_requests']) + 1
            row = {'number': number, 'method': method, 'route': route, 'content_type': content_type}
            report['http_requests'].append(row)
            if body is not None:
                data = body.encode() if isinstance(body, str) else body
                (output/f'provider-request-{number}.bin').write_bytes(data)
                row['request_file'] = f'provider-request-{number}.bin'
            try:
                raw = original_request(client, method, route, body, content_type)
                (output/f'provider-response-{number}.bin').write_bytes(raw)
                row['response_file'] = f'provider-response-{number}.bin'
                return raw
            except Exception as error:
                row.update(error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
                raise
            finally:
                save('provider-http.json', report['http_requests'])
        provider.render_sketch_image = observed_render
        provider.ComfyClient.request = observed_request
        def execute():
            try:
                report['image_operation_calls'] += 1
                report['operation_result'] = ops.generate_storyboard_panel(unit, shot_id, panel_id, board['revision'], PROMPT, simple_color=True)
            except Exception as error:
                report.update(error_type=type(error).__name__, error=str(error), raw_error_traceback=traceback.format_exc())
            finally:
                settled.set()
        began = time.monotonic()
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        settled.wait(args.seconds)
        report['elapsed_seconds'] = time.monotonic() - began
        report['image_jobs_submitted'] = sum(row['method'] == 'POST' and row['route'] == '/prompt' for row in report['http_requests'])
        checks = {'one_image_operation_only': report['image_operation_calls'] == 1,
            'one_actual_provider_function_call': len(report['provider_function_calls']) == 1}
        if report['provider_function_calls']:
            checks.update(assess_prompt(fixture['effective_style'], report['provider_function_calls'][0]['prompt'], PROMPT))
        if not settled.is_set():
            report.update(status='not_completed', stop_reason='External image QA observation expired; no retry, substitute image or result credited.')
        elif 'operation_result' not in report:
            report.update(status='not_completed', stop_reason='The real configured image service did not complete the generation; no mock or built-in imagegen substitute.')
            checks['failed_generation_preserves_board'] = ops.read_storyboard(unit) == before['board']
        else:
            after = ops.read_storyboard(unit)
            checks.update(assess_receipt(before['board'], after, fixture, report['provider_function_calls'], receipt))
            checks['one_image_job_submitted'] = report['image_jobs_submitted'] == 1
            report['status'] = 'passed' if all(checks.values()) else 'not_passed'
        if settled.is_set():
            checks.update(authored_screenplay_preserved=screenplay_content(ops.read_screenplay(fixture['screenplay_id'])) == before['screenplay'],
                extraction_preserved=ops.graph_snapshot() == before['graph'], adaptation_preserved=ops.adaptation_snapshot() == before['adaptation'],
                assets_preserved=all_assets(ops) == before['assets'])
            if report['status'] == 'passed' and not all(checks.values()):
                report['status'] = 'not_passed'
        report['checks'] = checks
    except Exception as error:
        report.update(status='not_completed', error_type=type(error).__name__, error=str(error), raw_error_traceback=traceback.format_exc())
    finally:
        provider.render_sketch_image = original_render
        provider.ComfyClient.request = original_request
        report['finished'] = datetime.now(UTC).isoformat()
        save('report.json', report)
        save('manifest.json', report)
    print(json.dumps({'status': report['status'], 'directory': str(output), 'provider_calls': len(report['provider_function_calls'])}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
