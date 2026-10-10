package com.shanghl.text2sql.dto;

/** 用户信息视图（hasKey = 是否已绑定 BYOK key；前端据此显示设置引导横幅）。 */
public record UserView(long id, String username, boolean hasKey) {
}
