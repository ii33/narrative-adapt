"""Current project sketch presentation, independent of chat prompt snapshots."""


def current_sketch_style(ops):
    # Image generation starts now; an old conversation snapshot must not hide
    # a project style edit during generation or later dependency review.
    return next(row['effective'] for row in ops.load_prompt_settings()['prompts'] if row['id'] == 'sketch_style')


def sketch_render_instructions(style, simple_color=False):
    mode = ('本次简彩：在简洁线稿基础上仅加少量清淡、叙事必要的色块，线条与简单结构优先，保留大量白底留白和既定光照；未知颜色不从灰阶擅猜，不画成满幅灰调或精细彩色插画。'
            if simple_color else '本次使用黑白灰线描工作分镜：少量清楚轮廓线与结构线、简单几何体、大量白底留白；灰面仅保留必要局部背光、投影和接触暗部，不自行添加色彩。')
    return ('统一草图呈现规范：\n' + style + '\n' + mode
            + '\n以少量线条和简单结构说明画面，不堆密集铅笔排线、纸纹、满幅灰调、精细肌肉衣褶或材质；仍保留既定光照所必需的小块阴影。此规范控制草图绘制方式。项目视觉风格、资产外观和参考图仅指导叙事内容、识别特征与既定光照，不能把草图改成精细插画或沿用参考板布局、文字。'
            + '\n只绘制当前时刻，清楚说明姿态、视线、手与道具接触和关键遮挡；不画文字、编号、箭头或界面。')
