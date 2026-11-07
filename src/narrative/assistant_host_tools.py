"""Host tools share Operations and require a program-bound full access task."""
from __future__ import annotations


def register(Tool, register_tool, validate):
    text = {'type': 'string'}
    integer = {'type': 'integer', 'minimum': 0}
    page = {'offset': integer, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 65536}}
    path = {'path': text | {'required': True, 'description': '本机绝对路径，允许工程外；业务数据库编辑仍应使用对应Operations业务工具'}}
    def add(name, summary, params, writes=False):
        def invoke(ops, args):
            for key, value in args.items():
                validate(value, params[key], key)
            try:
                return getattr(ops, name)(**args)
            except (ValueError, OSError, LookupError) as error:
                from .tool_api import ToolError
                raise ToolError(str(error)) from error
        register_tool(Tool(name, summary, params, invoke, writes=writes, scope='host'))
    add('read_host_file', '完全访问任务读取本机文本分页，自动识别编码/BOM并保留原换行，返回codec/eol；旧编码歧义可显式encoding，无损识别失败不猜。文本offset/limit按字符，encoding=base64按字节。文本上限8MiB，输出和审计脱敏', path | page | {'encoding': text})
    add('write_host_file', '完全访问任务原子写文本；默认保留原编码/BOM/换行，新文件UTF8/LF；不让模型拼Base64或shell重定向。append追加，eol可显式lf/crlf/cr。文本8MiB，不能编码或混合换行歧义明确失败不改原件；base64仅二进制。业务数据用Operations', path | {'content': text | {'required': True}, 'encoding': text, 'append': {'type': 'boolean'}, 'eol': {'type': 'string', 'enum': ['lf', 'crlf', 'cr']}}, True)
    add('edit_host_file', '完全访问任务精确文本替换。匹配时屏蔽LF/CRLF/CR差异，保原编码/BOM和未改区域；literal反斜杠不再次转义。默认必须唯一，显式replace_all才全替换；不存在/歧义/不能无损写回拒绝，不自动重试。业务数据仍用Operations', path | {'old_text': text | {'required': True}, 'new_text': text | {'required': True}, 'replace_all': {'type': 'boolean'}, 'encoding': text}, True)
    add('list_host_directory', '完全访问任务分页列出本机目录，不递归；offset按目录条目，结果脱敏', path | page)
    add('run_host_command', '完全访问任务异步执行系统shell文本（Windows默认cmd，非PowerShell）。普通程序优先run_host_process；脚本用run_host_script免转义。cwd须绝对路径；返回command-N。无命令总时限，任务终态收束进程树；显式background仅正常完成后移交', {
        'command': text | {'required': True}, 'cwd': text | {'required': True}, 'encoding': text, 'background': {'type': 'boolean'}}, True)
    process_options = {'cwd': text | {'description': '可省略，默认工程.nproj入口所在目录；显式值须绝对目录'},
        'stdin_text': text | {'description': '原样文本标准输入，可含多行，不拼入命令'}, 'input_encoding': text,
        'encoding': text | {'description': '输出编码，默认auto严格识别；歧义保留字节供read_host_command显式重解码，不重跑'}, 'background': {'type': 'boolean'}}
    add('run_host_process', '完全访问任务argv直连运行程序，不经shell；args每项原样，不添加引号/转义。返回command-N，经read/stop统一管理；无固定总时限', {
        'program': text | {'required': True}, 'args': {'type': 'array', 'items': text, 'required': True}} | process_options, True)
    add('run_host_script', '完全访问任务运行原始脚本文本；程序内部选解释器、创建UTF8脚本、处理路径/编码，禁止模型自行构造base64/EncodedCommand/heredoc。pwsh只使用7，不回退5.1/cmd。返回command-N，终态清理临时脚本并保留脱敏审计', {
        'language': {'type': 'string', 'enum': ['pwsh', 'python', 'sh'], 'required': True}, 'script': text | {'required': True}} | process_options, True)
    add('read_host_command', '完全访问任务分页读取当前任务命令或本工程后台服务的脱敏输出及实际退出码。终态可给encoding对保留字节重新严格解码，不重新执行命令；歧义明确反馈', {'command_id': text | {'required': True}, 'encoding': text} | page)
    add('stop_host_command', '完全访问任务停止指定command-N的进程树并等待收束，只作用当前任务或本工程已移交后台服务，不终止助手运行时', {'command_id': text | {'required': True}}, True)
