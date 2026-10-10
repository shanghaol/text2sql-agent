package com.shanghl.text2sql.service;

import java.nio.charset.StandardCharsets;

import javax.crypto.AEADBadTagException;
import javax.crypto.spec.SecretKeySpec;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * BYOK 核心单测（不起 Spring、不依赖 MySQL/外网，W4 §8 T1 验收）：
 * 加密→解密往返、密文≠明文、IV 两次加密不同、篡改密文抛异常、指纹/掩码。
 */
class KeyCipherServiceTest {

    private static final String KEY = "sk-0123456789abcdefghijklmnopqrstuvwxyz";

    private KeyCipherService cipher;

    @BeforeEach
    void setUp() {
        SecretKeySpec master = new SecretKeySpec(new byte[32], "AES"); // 32 字节全零密钥，测试专用
        cipher = new KeyCipherService(master);
    }

    @Test
    @DisplayName("加密→解密往返得到原文")
    void roundTrip() throws AEADBadTagException {
        KeyCipherService.SealedKey sealed = cipher.encrypt(KEY);
        assertThat(sealed.iv()).hasSize(12);
        assertThat(sealed.ciphertext()).hasSize(KEY.getBytes(StandardCharsets.UTF_8).length + 16); // 含 16B tag

        String decrypted = cipher.decrypt(sealed.iv(), sealed.ciphertext());
        assertThat(decrypted).isEqualTo(KEY);
    }

    @Test
    @DisplayName("密文不等于明文（防明文落库）")
    void ciphertextDiffersFromPlaintext() {
        KeyCipherService.SealedKey sealed = cipher.encrypt(KEY);
        assertThat(new String(sealed.ciphertext(), StandardCharsets.UTF_8)).doesNotContain(KEY);
        assertThat(sealed.ciphertext()).isNotEqualTo(KEY.getBytes(StandardCharsets.UTF_8));
    }

    @Test
    @DisplayName("IV 每次随机：同一明文两次加密 IV 与密文都不同（IV 纪律 §1.3）")
    void ivDiffersBetweenEncryptions() throws AEADBadTagException {
        KeyCipherService.SealedKey a = cipher.encrypt(KEY);
        KeyCipherService.SealedKey b = cipher.encrypt(KEY);
        assertThat(a.iv()).isNotEqualTo(b.iv());
        assertThat(a.ciphertext()).isNotEqualTo(b.ciphertext());
        // 且两者都能解回原文
        assertThat(cipher.decrypt(a.iv(), a.ciphertext())).isEqualTo(KEY);
        assertThat(cipher.decrypt(b.iv(), b.ciphertext())).isEqualTo(KEY);
    }

    @Test
    @DisplayName("篡改密文一个字节 → AEADBadTagException（GCM 认证加密完整性）")
    void tamperedCiphertextRejected() {
        KeyCipherService.SealedKey sealed = cipher.encrypt(KEY);
        byte[] tampered = sealed.ciphertext().clone();
        tampered[0] ^= 0x01;
        assertThatThrownBy(() -> cipher.decrypt(sealed.iv(), tampered))
                .isInstanceOf(AEADBadTagException.class);
    }

    @Test
    @DisplayName("篡改 IV → AEADBadTagException")
    void tamperedIvRejected() {
        KeyCipherService.SealedKey sealed = cipher.encrypt(KEY);
        byte[] tamperedIv = sealed.iv().clone();
        tamperedIv[0] ^= 0x01;
        assertThatThrownBy(() -> cipher.decrypt(tamperedIv, sealed.ciphertext()))
                .isInstanceOf(AEADBadTagException.class);
    }

    @Test
    @DisplayName("主密钥不匹配（换环境忘迁 BYOK_MASTER_KEY）→ AEADBadTagException")
    void wrongMasterKeyRejected() throws AEADBadTagException {
        KeyCipherService.SealedKey sealed = cipher.encrypt(KEY);
        byte[] otherKeyBytes = new byte[32];
        java.util.Arrays.fill(otherKeyBytes, (byte) 0x5A); // 与加密用的全零密钥不同
        KeyCipherService other = new KeyCipherService(new SecretKeySpec(otherKeyBytes, "AES"));
        assertThatThrownBy(() -> other.decrypt(sealed.iv(), sealed.ciphertext()))
                .isInstanceOf(AEADBadTagException.class);
    }

    @Test
    @DisplayName("指纹 = SHA-256 前 8 hex，确定且稳定（§1.5 规则 2 日志替身）")
    void fingerprintIsStable8Hex() {
        String fp1 = KeyCipherService.fingerprint(KEY);
        String fp2 = KeyCipherService.fingerprint(KEY);
        assertThat(fp1).isEqualTo(fp2).hasSize(8).matches("[0-9a-f]{8}");
        assertThat(KeyCipherService.fingerprint(KEY + "x")).isNotEqualTo(fp1);
    }

    @Test
    @DisplayName("掩码 = sk-**** + 后 4 位（§1.2 步骤 5）")
    void maskKeepsLast4() {
        assertThat(KeyCipherService.mask(KEY)).isEqualTo("sk-****" + KEY.substring(KEY.length() - 4));
        assertThat(KeyCipherService.mask("ab")).isEqualTo("sk-****");
    }
}
