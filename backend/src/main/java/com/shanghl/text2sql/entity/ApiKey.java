package com.shanghl.text2sql.entity;

import java.time.LocalDateTime;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * BYOK 密钥表实体（一人一行，uk_api_key_user）。
 *
 * 日志脱敏红线（设计 §1.5）：本实体的 key_ciphertext/key_iv 是二进制密文材料，
 * key_masked/key_fingerprint 是可入响应/日志的替身——**任何日志与接口响应都不得出现明文 key**，
 * 明文只存在于加密/解密调用的局部变量里（§1.3，显式加密点见 KeyCipherService）。
 */
@TableName("api_key")
public class ApiKey {

    @TableId(type = IdType.AUTO)
    private Long id;

    @TableField("user_id")
    private Long userId;

    @TableField("key_ciphertext")
    private byte[] keyCiphertext;

    @TableField("key_iv")
    private byte[] keyIv;

    @TableField("key_fingerprint")
    private String keyFingerprint;

    @TableField("key_masked")
    private String keyMasked;

    @TableField("key_version")
    private Integer keyVersion;

    @TableField("last_verified_at")
    private LocalDateTime lastVerifiedAt;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
    public byte[] getKeyCiphertext() { return keyCiphertext; }
    public void setKeyCiphertext(byte[] keyCiphertext) { this.keyCiphertext = keyCiphertext; }
    public byte[] getKeyIv() { return keyIv; }
    public void setKeyIv(byte[] keyIv) { this.keyIv = keyIv; }
    public String getKeyFingerprint() { return keyFingerprint; }
    public void setKeyFingerprint(String keyFingerprint) { this.keyFingerprint = keyFingerprint; }
    public String getKeyMasked() { return keyMasked; }
    public void setKeyMasked(String keyMasked) { this.keyMasked = keyMasked; }
    public Integer getKeyVersion() { return keyVersion; }
    public void setKeyVersion(Integer keyVersion) { this.keyVersion = keyVersion; }
    public LocalDateTime getLastVerifiedAt() { return lastVerifiedAt; }
    public void setLastVerifiedAt(LocalDateTime lastVerifiedAt) { this.lastVerifiedAt = lastVerifiedAt; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
    public LocalDateTime getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(LocalDateTime updatedAt) { this.updatedAt = updatedAt; }
}
