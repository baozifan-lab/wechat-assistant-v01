"""Small, binding-scoped task choices instead of asking people to copy run IDs."""
import hashlib
import json
import re


def channel_key(channel, route):
    return hashlib.sha256(json.dumps([channel,route],sort_keys=True).encode()).hexdigest()


def choice_index(text, count):
    text = text.strip().rstrip('。！!').replace(' ', '')
    if count == 1 and text in {'是这件','就是这件','是这个','就这个','对，就是这件','对'}:
        return 0
    match = re.fullmatch(r'(?:就|选|选择)?(?:第|任务)?([1-9一二三四五六七八九])(?:个任务|项任务|件事|个|项|件|号|单)?(?:吧)?',text)
    if not match:
        return None
    value = match[1]
    return int(value)-1 if value.isdigit() else '一二三四五六七八九'.index(value)


def choice_prompt(options, other_work=False):
    lines = ['你是在回复哪一件工作？我先保留你刚才的话，不会确认到别的任务。']
    for index,item in enumerate(options,1):
        lines.append(f'{index}. {item["label"]}')
    if other_work:
        lines.append('当前还有其他业务流程在等待回复；如果是其他工作，请说“不是这件”。')
    lines.append('直接说“是这件”或“第一个”即可；不用复制长编号。' if len(options)==1 else
                 '直接说“第一个”“第二个”，也可以回选项数字；不用复制长编号。')
    return '\n'.join(lines)
