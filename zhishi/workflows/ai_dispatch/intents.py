"""Exact workflow commands avoid an unnecessary remote model request."""
import re


def explicit_intent(state, text):
    text = text.strip().rstrip('。！!').strip().replace(',', '，').lower()
    receipt = {'收到','收到啦','已收到','了解','知道了','明白了','我先看看','等我一下','稍等一下'}
    approval = {'好','好的','可以','行','没问题','没有问题','ok','okay','可以继续','继续吧','安排吧','就这么办','按这个来'}
    action = None
    if state == 'manager_wait':
        if text in approval | {'确认','同意','同意，开始吧','同意开始','开始吧','确认启动','可以开始','可以，开始吧'}:
            action = 'accept'
        elif text in receipt:
            action = 'acknowledge'
    elif state in ('employee1_wait','employee2_wait'):
        if state == 'employee1_wait' and text in receipt | approval | {'接单','我接单','我来准备','我来导出','我去导出','收到，我来准备'}:
            action = 'acknowledge'
        elif state == 'employee1_wait' and text in {'开始吧','同意，开始吧'}:
            action = 'help'
        elif state == 'employee2_wait' and text in receipt | approval | {'开始吧','帮我查一下','查吧','对比一下','比对一下','开始比对','执行商品对比'}:
            action = 'help'
        elif text in {'接单','我接单','开始处理','同意，开始吧','开始吧'}:
            action = 'accept'
        elif text in {'已完成','完成了','都做好了','文件已导出','已导出','好了','弄好了','导好了','准备好了','文件放好了','搞定了'}:
            # A completion claim starts the registered checker; it is never a
            # substitute for verified output and a separate review confirmation.
            action = 'complete'
        elif (text in {'检查输入','请检查输入','执行商品比对','请执行商品比对','开始检查','帮我检查一下','检查一下','请检查文件'}
              or re.fullmatch(r'(?:文件已导出[， ]*)?(?:请|帮我)?检查(?:一下)?(?:已导出的|现有的|已有的)?(?:输入)?文件(?:[， ]*(?:并|然后)?继续)?', text)):
            action = 'help'
    elif state in ('employee1_review','employee2_review'):
        if text in receipt:
            action = 'acknowledge'
        elif text in approval | {'已核对，结果无误','已核对，确认无误','我已核对本版成果，结果无误',
                                 '本版结果核对无误','确认本版结果无误','核对过了','核对好了','确认了','结果没问题','对的','没错','无误'}:
            action = 'complete'
    if action is None:
        return None
    return {'action':action,'confidence':1.0,'reason':'本阶段明确指令，由本地规则识别；是否完成仍由技能和成果校验决定。'}
