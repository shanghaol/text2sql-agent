package com.shanghl.text2sql.service;

import java.time.Clock;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.dto.AuthData;
import com.shanghl.text2sql.dto.LoginRequest;
import com.shanghl.text2sql.dto.RefreshRequest;
import com.shanghl.text2sql.dto.RegisterRequest;
import com.shanghl.text2sql.dto.UserView;
import com.shanghl.text2sql.entity.ApiKey;
import com.shanghl.text2sql.entity.User;
import com.shanghl.text2sql.mapper.ApiKeyMapper;
import com.shanghl.text2sql.mapper.UserMapper;
import com.shanghl.text2sql.security.JwtService;

import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;

/**
 * 注册 / 登录 / refresh 旋转（设计 §2.3）。
 * - 注册：用户名冲突 → 409 username_taken；注册即登录（直接发 token，省一步）。
 * - 登录：BCrypt 校验；用户不存在时也对 dummy 哈希跑一次 matches——抹平"用户不存在"
 *   与"密码错误"的响应时间差（防用户名枚举的时序侧信道）。
 * - refresh：旋转换发（签发全新双 token）；typ=refresh 之外一律 401 invalid_refresh。
 * 已知限制（§2.3 写明）：无状态 refresh 无法服务端吊销，登出=客户端丢弃。
 */
@Service
public class AuthService {

    /** 时序防御用的 dummy BCrypt 哈希（构造时对固定串生成一次）。 */
    private static final String DUMMY_PASSWORD = "timing-defender-dummy";

    private final UserMapper userMapper;
    private final ApiKeyMapper apiKeyMapper;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;
    private final Clock clock;
    private final String dummyHash;

    public AuthService(UserMapper userMapper, ApiKeyMapper apiKeyMapper,
                       PasswordEncoder passwordEncoder, JwtService jwtService, Clock clock) {
        this.userMapper = userMapper;
        this.apiKeyMapper = apiKeyMapper;
        this.passwordEncoder = passwordEncoder;
        this.jwtService = jwtService;
        this.clock = clock;
        this.dummyHash = passwordEncoder.encode(DUMMY_PASSWORD);
    }

    public AuthData register(RegisterRequest req) {
        Long existing = userMapper.selectCount(Wrappers.<User>lambdaQuery().eq(User::getUsername, req.username()));
        if (existing != null && existing > 0) {
            throw new BizException(ErrorCode.USERNAME_TAKEN);
        }
        User user = new User();
        user.setUsername(req.username());
        user.setPasswordHash(passwordEncoder.encode(req.password()));
        userMapper.insert(user); // IdType.AUTO 回填 user.id

        return issueTokens(user);
    }

    public AuthData login(LoginRequest req) {
        User user = userMapper.selectOne(Wrappers.<User>lambdaQuery().eq(User::getUsername, req.username()));
        boolean ok = user != null && passwordEncoder.matches(req.password(), user.getPasswordHash());
        if (!ok) {
            if (user == null) {
                // 用户不存在：同样跑一次 BCrypt（dummy 哈希），避免响应时间暴露用户名是否存在
                passwordEncoder.matches(req.password(), dummyHash);
            }
            throw new BizException(ErrorCode.BAD_CREDENTIALS);
        }
        return issueTokens(user);
    }

    public AuthData refresh(RefreshRequest req) {
        Claims claims;
        try {
            claims = jwtService.parseRefresh(req.refreshToken());
        } catch (JwtException | IllegalArgumentException e) {
            throw new BizException(ErrorCode.INVALID_REFRESH);
        }
        long userId = Long.parseLong(claims.getSubject());
        User user = userMapper.selectById(userId);
        if (user == null) {
            // token 有效但用户已不存在（自建 demo 无删除入口，防御性）
            throw new BizException(ErrorCode.INVALID_REFRESH);
        }
        return issueTokens(user);
    }

    public UserView me(long userId) {
        User user = userMapper.selectById(userId);
        if (user == null) {
            throw new BizException(ErrorCode.AUTH_REQUIRED);
        }
        return new UserView(user.getId(), user.getUsername(), hasKey(user.getId()));
    }

    boolean hasKey(long userId) {
        Long count = apiKeyMapper.selectCount(Wrappers.<ApiKey>lambdaQuery().eq(ApiKey::getUserId, userId));
        return count != null && count > 0;
    }

    private AuthData issueTokens(User user) {
        String access = jwtService.issueAccess(user.getId(), user.getUsername());
        String refresh = jwtService.issueRefresh(user.getId(), user.getUsername());
        UserView view = new UserView(user.getId(), user.getUsername(), hasKey(user.getId()));
        return new AuthData(user.getId(), access, refresh, view);
    }

    /** 测试可见性：时钟（当前未直接使用，保留给未来需要时间戳的逻辑）。 */
    Clock clock() {
        return clock;
    }
}
