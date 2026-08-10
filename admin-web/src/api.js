import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from './router'

const api = axios.create({ baseURL: '/admin/api', timeout: 30000 })

api.interceptors.request.use(cfg => {
  const t = localStorage.getItem('admin_token')
  if (t) cfg.headers['X-Admin-Token'] = t
  return cfg
})

api.interceptors.response.use(
  resp => {
    const j = resp.data
    if (j.code !== 0) {
      if (j.code === 4004) { localStorage.removeItem('admin_token'); router.push('/login') }
      ElMessage.error(j.msg || '请求失败')
      return Promise.reject(new Error(j.msg))
    }
    return j.data
  },
  err => { ElMessage.error('网络异常'); return Promise.reject(err) }
)

export default api
