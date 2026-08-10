import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes: [
    { path: '/login', component: () => import('./views/Login.vue') },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      children: [
        { path: '', redirect: '/review' },
        { path: 'review', component: () => import('./views/ReviewQueue.vue') },
        { path: 'review/:id', component: () => import('./views/ChapterReview.vue') },
        { path: 'themes', component: () => import('./views/Themes.vue') },
        { path: 'books', component: () => import('./views/Books.vue') },
      ]
    }
  ]
})

router.beforeEach((to) => {
  const token = localStorage.getItem('admin_token')
  if (!token && to.path !== '/login') return '/login'
})

export default router
