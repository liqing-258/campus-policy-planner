<script setup lang="ts">
/**
 * `/login` 兼容页 —— 只是把落地页的登录小窗直接拉开。
 *
 * 保留这条路由的原因不是"懒得删"：旧书签、`start-backend.bat` 打开的地址、
 * 答辩脚本与 CDP 验收都写着 `/login`。删掉之后它们会掉进未匹配路由被守卫弹回，
 * 现象是"登录入口没了"。
 *
 * **不再自己画表单**：R9 之前这里是左右分栏的大卡片，与 `AuthPanel` 同时存在
 * 会有两套登录实现，改一处忘一处。现在它把 `autoOpen` 交给落地页，
 * 全仓只有 `AuthPanel.vue` 一个地方有密码输入框。
 */
import WelcomeView from './WelcomeView.vue'
</script>

<template>
  <WelcomeView :auto-open="true" />
</template>
