"""安伴运行配置的默认值与不可突破的安全上限。"""

# 单次模型请求超时，单位为秒；默认值可由用户调整，硬上限避免单次请求占满执行预算。
MODEL_REQUEST_TIMEOUT_DEFAULT_SECONDS = 60
# 单次模型请求超时的最小值，单位为秒；避免无意义的零或负超时。
MODEL_REQUEST_TIMEOUT_MIN_SECONDS = 1
# 单次模型请求超时的安全硬上限，单位为秒；用户配置不得突破。
MODEL_REQUEST_TIMEOUT_MAX_SECONDS = 120

# 临时传输错误的默认重试次数，不包含首次请求；用户可以在安全范围内调整。
MODEL_TRANSPORT_RETRIES_DEFAULT = 2
# 传输重试次数的最小值；零表示明确禁用自动重试。
MODEL_TRANSPORT_RETRIES_MIN = 0
# 传输重试次数的安全硬上限；限制网络放大和总执行时间。
MODEL_TRANSPORT_RETRIES_MAX = 3

# 非法模型响应的默认结构修复次数；这是单个 Agent Node 共用的预算。
MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT = 3
# 响应修复次数的最小值；零表示保持首次非法即失败。
MODEL_RESPONSE_REPAIR_RETRIES_MIN = 0
# 响应修复次数的安全硬上限；避免无界模型循环且不允许用户突破。
MODEL_RESPONSE_REPAIR_RETRIES_MAX = 3

# 单个 Agent Node 的默认模型逻辑轮次；用户可调但不得超过安全上限。
AGENT_MAX_MODEL_TURNS_DEFAULT = 8
# 模型逻辑轮次的最小值；至少允许一次真实模型请求。
AGENT_MAX_MODEL_TURNS_MIN = 1
# 模型逻辑轮次的安全硬上限；包含结构修复请求。
AGENT_MAX_MODEL_TURNS_MAX = 8

# 单个 Agent Node 的默认 Capability 调用次数；用户可在范围内调低。
AGENT_MAX_CAPABILITY_CALLS_DEFAULT = 8
# Capability 调用次数的最小值；保持可执行 Agent 的基本能力。
AGENT_MAX_CAPABILITY_CALLS_MIN = 1
# Capability 调用次数的安全硬上限；限制副作用与资源消耗。
AGENT_MAX_CAPABILITY_CALLS_MAX = 8

# 单次 Agent 执行的默认总超时，单位为秒；覆盖模型、重试和 Capability。
AGENT_TOTAL_TIMEOUT_DEFAULT_SECONDS = 180
# Agent 总超时的最小值，单位为秒；拒绝零或负时间预算。
AGENT_TOTAL_TIMEOUT_MIN_SECONDS = 1
# Agent 总超时的安全硬上限，单位为秒；所有内部重试均受其约束。
AGENT_TOTAL_TIMEOUT_MAX_SECONDS = 180

# 连续相同 Capability 调用的默认终止阈值；第三次执行前失败。
AGENT_REPEATED_CALL_LIMIT_DEFAULT = 3
# 连续相同调用阈值的最小值；允许更严格地在第二次前终止。
AGENT_REPEATED_CALL_LIMIT_MIN = 2
# 连续相同调用阈值的安全硬上限；防止重复副作用。
AGENT_REPEATED_CALL_LIMIT_MAX = 3

# process.execute 的默认超时，单位为秒；用户可在安全范围内调整。
PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS = 10
# process.execute 默认超时的最小值，单位为秒。
PROCESS_DEFAULT_TIMEOUT_MIN_SECONDS = 1
# process.execute 超时参数及默认值的安全硬上限，单位为秒。
PROCESS_TIMEOUT_MAX_SECONDS = 30

# process.execute 最多保留的 stdout 字节数；固定硬上限防止内存与输出放大。
PROCESS_STDOUT_MAX_BYTES = 16_384
# process.execute 最多保留的 stderr 字节数；固定硬上限防止敏感或无界输出。
PROCESS_STDERR_MAX_BYTES = 16_384
# process.execute 允许的最大参数数量；固定硬上限防止命令放大。
PROCESS_ARGUMENTS_MAX = 64

# 受治理 HTTP GET 的固定最长执行时间，单位为秒；不可由 Workspace 放宽。
HTTP_GET_TIMEOUT_MAX_SECONDS = 30
# 受治理 HTTP GET 的固定最大响应字节数；避免下载和模型上下文放大。
HTTP_GET_RESPONSE_MAX_BYTES = 16_384
# 受治理 HTTP GET 的最大 URL 长度；限制解析和请求表面积。
HTTP_GET_URL_MAX_LENGTH = 2_048
