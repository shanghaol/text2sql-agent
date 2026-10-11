package com.shanghl.text2sql.service;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.dto.UserSettingsView;
import com.shanghl.text2sql.entity.User;
import com.shanghl.text2sql.mapper.UserMapper;

import org.springframework.stereotype.Service;

/**
 * 用户设置（W5 设计 §3.8）：目前只有 HITL 一档（是否在 SQL 执行前人工确认）。
 * 存储 = user 表 hitl_enabled 列（TINYINT 0/1，默认 0）——单开关、用户维度、无扩展诉求，
 * 加列不加表（§3.8 决策）。列由 deploy/w5_migrate_user_settings.sh 守护迁移补齐。
 */
@Service
public class SettingsService {

    private final UserMapper userMapper;

    public SettingsService(UserMapper userMapper) {
        this.userMapper = userMapper;
    }

    public UserSettingsView get(long userId) {
        return new UserSettingsView(hitlEnabled(userId));
    }

    public UserSettingsView update(long userId, boolean hitlEnabled) {
        User user = requireUser(userId);
        user.setHitlEnabled(hitlEnabled ? 1 : 0);
        // 只改一列：UpdateWrapper set 限定列，避免全实体回写（username/password_hash 原值冗余写）
        userMapper.update(null, Wrappers.<User>lambdaUpdate()
                .eq(User::getId, userId)
                .set(User::getHitlEnabled, hitlEnabled ? 1 : 0));
        return new UserSettingsView(hitlEnabled(userId));
    }

    /** 代理层（ChatProxyService.open）注入 X-User-HITL 头前的同一判定口径。 */
    public boolean hitlEnabled(long userId) {
        User user = requireUser(userId);
        return Integer.valueOf(1).equals(user.getHitlEnabled());
    }

    private User requireUser(long userId) {
        User user = userMapper.selectById(userId);
        if (user == null) {
            // JWT 已验身份，用户行消失属异常态：按未认证处理，不泄露细节
            throw new BizException(ErrorCode.AUTH_REQUIRED);
        }
        return user;
    }
}
