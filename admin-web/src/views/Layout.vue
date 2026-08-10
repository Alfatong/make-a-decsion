<template>
  <el-container style="min-height:100vh">
    <el-aside width="200px" style="background:#001529">
      <div style="color:#fff;font-size:17px;font-weight:700;padding:20px;text-align:center">小说后台</div>
      <el-menu :default-active="$route.path" router background-color="#001529"
               text-color="#a6adb4" active-text-color="#fff">
        <el-menu-item index="/review">审核队列</el-menu-item>
        <el-menu-item index="/themes">题材管理</el-menu-item>
        <el-menu-item index="/books">书籍管理</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background:#fff;border-bottom:1px solid #eee;display:flex;justify-content:flex-end;align-items:center">
        <el-button text @click="logout">退出登录</el-button>
      </el-header>
      <el-main style="background:#f0f2f5">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
async function logout() {
  try { await api.post('/logout') } catch (e) { /* 忽略 */ }
  localStorage.removeItem('admin_token')
  router.push('/login')
}
</script>
