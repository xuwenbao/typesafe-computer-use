"""为什么一次运行停下来。下一步交给谁写在 `Status` 里，原因写在这里。"""

from __future__ import annotations

# 分类器停下的各种原因，以及发给写手的英文说明（写手会据此向用户解释）。
# 演练（dry run，不真的操作）和用户中止不在这里，它们不交给写手。
CLASSIFIER_STOPPED = {
    "done": "the classifier judged the goal already achieved on this screen",
    "nothing helps": "the classifier found nothing on this screen that helps with the goal",
    "low confidence": "the classifier was not confident enough in any next action",
    "malformed answer": "the classifier's answer did not match the options it was offered, so nothing was done",
    "stalled": "the last actions changed nothing",
    "stuck": (
        "the agent is stuck: its last actions changed nothing, as they did twice before on these same pages, "
        "and the focus given each time did not help, so the run ends with this answer"
    ),
    "step limit": "the run used every step it was allowed",
}

# 接管结束时告诉写手的英文说明。写手用它向用户解释这次是怎么停的，所以保持英文。
_TOOK_OVER = "the classifier stopped short of the goal for good, a stronger model took over the actions, and "
DRIVER_STOPPED = {
    "driver done": _TOOK_OVER + "it judged the goal reached on this screen",
    "driver gave up": _TOOK_OVER + "it found no action that would get closer to the goal",
    "driver stalled": _TOOK_OVER + "its last actions changed nothing",
    "driver step limit": _TOOK_OVER + "it used every action it was allowed",
    "driver failed": _TOOK_OVER + "the model it asks could not be reached",
}
