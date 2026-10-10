package com.shanghl.text2sql.config;

import java.time.Duration;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 应用配置（application.yml 的 app.* 前缀）。
 * 密钥类（jwt.secret / byok.master-key）只经环境变量注入，yml 中缺省为空串。
 */
@ConfigurationProperties(prefix = "app")
public class AppProperties {

    private Jwt jwt = new Jwt();
    private Byok byok = new Byok();
    private String internalToken = "";
    private Python python = new Python();
    private Deepseek deepseek = new Deepseek();
    private Chat chat = new Chat();

    public Jwt getJwt() { return jwt; }
    public void setJwt(Jwt jwt) { this.jwt = jwt; }
    public Byok getByok() { return byok; }
    public void setByok(Byok byok) { this.byok = byok; }
    public String getInternalToken() { return internalToken; }
    public void setInternalToken(String internalToken) { this.internalToken = internalToken; }
    public Python getPython() { return python; }
    public void setPython(Python python) { this.python = python; }
    public Deepseek getDeepseek() { return deepseek; }
    public void setDeepseek(Deepseek deepseek) { this.deepseek = deepseek; }
    public Chat getChat() { return chat; }
    public void setChat(Chat chat) { this.chat = chat; }

    public static class Jwt {
        private String secret = "";
        private Duration accessTtl = Duration.ofHours(2);   // 设计 §2.3：access 2h
        private Duration refreshTtl = Duration.ofDays(7);   // refresh 7d

        public String getSecret() { return secret; }
        public void setSecret(String secret) { this.secret = secret; }
        public Duration getAccessTtl() { return accessTtl; }
        public void setAccessTtl(Duration accessTtl) { this.accessTtl = accessTtl; }
        public Duration getRefreshTtl() { return refreshTtl; }
        public void setRefreshTtl(Duration refreshTtl) { this.refreshTtl = refreshTtl; }
    }

    public static class Byok {
        private String masterKey = "";

        public String getMasterKey() { return masterKey; }
        public void setMasterKey(String masterKey) { this.masterKey = masterKey; }
    }

    public static class Python {
        private String baseUrl = "http://localhost:8000";

        public String getBaseUrl() { return baseUrl; }
        public void setBaseUrl(String baseUrl) { this.baseUrl = baseUrl; }
    }

    public static class Deepseek {
        private String baseUrl = "https://api.deepseek.com";
        private Duration verifyTimeout = Duration.ofSeconds(5); // GET /models 校验超时（§1.2）

        public String getBaseUrl() { return baseUrl; }
        public void setBaseUrl(String baseUrl) { this.baseUrl = baseUrl; }
        public Duration getVerifyTimeout() { return verifyTimeout; }
        public void setVerifyTimeout(Duration verifyTimeout) { this.verifyTimeout = verifyTimeout; }
    }

    public static class Chat {
        private int maxQuestionChars = 2000;
        private int rateLimitPerMin = 10;             // 对话 10/min/用户（§1.7）
        private int keyVerifyRateLimitPerMin = 5;     // key 校验 5/min/用户
        private int authRateLimitPerMin = 10;         // 登录/注册 10/min/IP
        private int maxConcurrentStreams = 4;         // 全局并发流（2核2G 上界）

        public int getMaxQuestionChars() { return maxQuestionChars; }
        public void setMaxQuestionChars(int maxQuestionChars) { this.maxQuestionChars = maxQuestionChars; }
        public int getRateLimitPerMin() { return rateLimitPerMin; }
        public void setRateLimitPerMin(int rateLimitPerMin) { this.rateLimitPerMin = rateLimitPerMin; }
        public int getKeyVerifyRateLimitPerMin() { return keyVerifyRateLimitPerMin; }
        public void setKeyVerifyRateLimitPerMin(int keyVerifyRateLimitPerMin) { this.keyVerifyRateLimitPerMin = keyVerifyRateLimitPerMin; }
        public int getAuthRateLimitPerMin() { return authRateLimitPerMin; }
        public void setAuthRateLimitPerMin(int authRateLimitPerMin) { this.authRateLimitPerMin = authRateLimitPerMin; }
        public int getMaxConcurrentStreams() { return maxConcurrentStreams; }
        public void setMaxConcurrentStreams(int maxConcurrentStreams) { this.maxConcurrentStreams = maxConcurrentStreams; }
    }
}
