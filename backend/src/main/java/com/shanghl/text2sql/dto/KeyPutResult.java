package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/** BYOK 录入成功响应（§2.4）：只含掩码/指纹/校验时间，永不回明文 key。 */
public record KeyPutResult(String masked, String fingerprint, LocalDateTime lastVerifiedAt) {
}
