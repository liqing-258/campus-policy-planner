<script setup lang="ts">
/**
 * `/login` 兼容页 —— 与 `/welcome` 同一个落地页，**默认不拉开登录小窗**。
 *
 * 保留这条路由的原因不是"懒得删"：旧书签、`start-backend.bat` 打开的地址、
 * 答辩脚本与 CDP 验收都写着 `/login`。删掉之后它们会掉进未匹配路由被守卫弹回，
 * 现象是"登录入口没了"。
 *
 * **R23 改掉"自动开窗"**：用户要求双击登录网址先看到首屏（系统名 + 开启系统 +
 * 知识库规模），而不是一上来就被登录框糊一脸。要直接开窗的场景（验收脚本、
 * "去登录"深链）走 `?open=1`——判据在 WelcomeView 里，这一层只把落地页挂出来。
 *
 * **不再自己画表单**：R9 之前这里是左右分栏的大卡片，与 `AuthPanel` 同时存在
 * 会有两套登录实现，改一处忘一处。全仓只有 `AuthPanel.vue` 一个地方有密码输入框。
 */
import WelcomeView from './WelcomeView.vue'
</script>

<template>
  <WelcomeView />
</template>
