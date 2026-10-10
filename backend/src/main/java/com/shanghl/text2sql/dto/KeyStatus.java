package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/**
 * BYOK 查询响应（§2.4）：configured=false 时其余字段为 null。
 * 本接口只给掩码与指纹——任何接口永不返回明文 key（设计 §1.5 规则）。
 */
public record KeyStatus(boolean configured, String masked, String fingerprint, LocalDateTime lastVerifiedAt) {
}
