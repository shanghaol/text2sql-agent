package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/** BYOK 复检响应（§2.4 设置页"重新校验"按钮）：对已存 key 重跑 DeepSeek /models。 */
public record VerifyKeyResult(boolean valid, LocalDateTime verifiedAt) {
}
