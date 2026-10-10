package com.shanghl.text2sql.service;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.AEADBadTagException;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Component;

/**
 * BYOK 主方案的核心：AES-256-GCM 加密落库（设计 §1.3，编码按设计骨架逐行实现）。
 *
 * ========================= 显式加密点 = 可讲解点（设计 §1.3 明文要求） =========================
 * 加解密发生在 KeyService / ChatProxyService 服务层显式调用，绝不藏进 JPA Converter /
 * MyBatis TypeHandler 魔法里——用户要求能讲清每一行，加密时机必须肉眼可见。
 *
 * 为什么是 AES-GCM 而不是 AES-CBC / 只存哈希（§1.3）：
 * - GCM 是认证加密：密文被篡改一个字节，解密直接抛 AEADBadTagException——
 *   拖库者无法静默伪造，这是 CBC（无内建完整性）给不了的；
 * - JDK 内置（javax.crypto.Cipher + SecretKeySpec），零第三方依赖；
 * - "只存哈希"被否：哈希不可逆，而对话时必须拿回明文 key（哈希只用于指纹这个辅助字段）。
 *
 * ========================= IV 纪律（面试可讲的密码学细节） =========================
 * **GCM 的 IV 复用是灾难性错误**：同一主密钥下 IV 重用会泄漏明文异或（keystream 重用）。
 * 因此 IV 必须每次用 SecureRandom 重新生成 12 字节，并与密文同存（DB key_iv 列），
 * 解密时原样取出——IV 绝不能缓存、复用或由确定性函数生成。
 */
@Component
public class KeyCipherService {

    private static final String TRANSFORMATION = "AES/GCM/NoPadding";
    private static final int GCM_TAG_BITS = 128;   // 认证标签 128 位（设计 §1.3 骨架）
    private static final int GCM_IV_BYTES = 12;    // GCM 标准 IV 长度 96 位
    private static final char[] HEX = "0123456789abcdef".toCharArray();

    private final SecretKeySpec masterKey;
    // SecureRandom 本身线程安全；Cipher 不是线程安全的 → 每次 encrypt/decrypt 新建实例
    private final SecureRandom secureRandom = new SecureRandom();

    public KeyCipherService(@Qualifier("byokMasterKey") SecretKeySpec masterKey) {
        this.masterKey = masterKey;
    }

    /** 加密结果：IV 与密文（密文已含 16 字节 tag）成对保存，落库到 key_iv / key_ciphertext。 */
    public record SealedKey(byte[] iv, byte[] ciphertext) {
    }

    public SealedKey encrypt(String plaintext) {
        // —— 加密：IV 每次重新随机生成（12 字节），GCM tag 128 位（§1.3 骨架原文）——
        byte[] iv = new byte[GCM_IV_BYTES];
        secureRandom.nextBytes(iv); // IV 复用是灾难性错误，见类注释
        try {
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.ENCRYPT_MODE, masterKey, new GCMParameterSpec(GCM_TAG_BITS, iv));
            byte[] ciphertext = cipher.doFinal(plaintext.getBytes(StandardCharsets.UTF_8)); // doFinal 结果已含 16 字节 tag
            return new SealedKey(iv, ciphertext);
        } catch (GeneralSecurityException e) {
            // AES/GCM 在标准 JDK 上必然可用；走到这里说明 JVM 环境异常（如被裁剪过的 JCE）
            throw new IllegalStateException("AES-GCM 加密失败：当前 JVM 不支持该算法，属环境异常", e);
        }
    }

    /**
     * 解密。密文被篡改（拖库后改字节）或主密钥不匹配（换环境忘迁 BYOK_MASTER_KEY，§7 风险 6）
     * 时抛 {@link AEADBadTagException}——由调用方映射为业务错误
     * "主密钥不匹配，请联系管理员重置 key"（MASTER_KEY_MISMATCH）。
     */
    public String decrypt(byte[] iv, byte[] ciphertext) throws AEADBadTagException {
        try {
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.DECRYPT_MODE, masterKey, new GCMParameterSpec(GCM_TAG_BITS, iv));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (AEADBadTagException e) {
            throw e; // 认证失败的唯一正常来源，直接上抛给调用方做业务映射
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException("AES-GCM 解密失败：当前 JVM 不支持该算法，属环境异常", e);
        }
    }

    /**
     * 指纹 = SHA-256(明文) 前 8 hex（设计 §1.5 规则 2）。
     * key 的唯一日志替身：`key saved userId=42 fp=1a2b3c4d len=35`；指纹同时落库，
     * 滥用排查按指纹反查用户而无需解密。
     */
    public static String fingerprint(String plaintext) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(plaintext.getBytes(StandardCharsets.UTF_8));
            char[] out = new char[8];
            for (int i = 0; i < 4; i++) {
                out[i * 2] = HEX[(digest[i] >> 4) & 0xF];
                out[i * 2 + 1] = HEX[digest[i] & 0xF];
            }
            return new String(out);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 不可用，属 JVM 环境异常", e);
        }
    }

    /** 掩码展示（§1.2 步骤 5）：如 sk-****ab3d。接口只返回此列，绝不返回明文。 */
    public static String mask(String plaintext) {
        if (plaintext.length() < 4) {
            return "sk-****";
        }
        return "sk-****" + plaintext.substring(plaintext.length() - 4);
    }
}
