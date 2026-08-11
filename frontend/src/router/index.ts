import { createRouter, createWebHistory } from 'vue-router'

const Home = () => import('../views/Home.vue')
const Workbench = () => import('../views/Workbench.vue')
const Chapter = () => import('../views/Chapter.vue')
const Cast = () => import('../views/Cast.vue')
const CharacterGraph = () => import('../views/CharacterGraph.vue')
const LocationGraph = () => import('../views/LocationGraph.vue')
const OutlineStudio = () => import('../views/OutlineStudio.vue')
const CandidateReviewDesk = () => import('../views/CandidateReviewDesk.vue')
const WorldlineRegeneration = () => import('../views/WorldlineRegeneration.vue')
const CharacterSchedulerSimulator = () =>
  import('../components/debug/CharacterSchedulerSimulator.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'Home', component: Home },
    { path: '/book/:slug/workbench', name: 'Workbench', component: Workbench },
    { path: '/book/:slug/cast', name: 'Cast', component: Cast },
    { path: '/book/:slug/chapter/:id', name: 'Chapter', component: Chapter },
    { path: '/book/:slug/characters', name: 'CharacterGraph', component: CharacterGraph },
    { path: '/book/:slug/location-graph', name: 'LocationGraph', component: LocationGraph },
    { path: '/book/:slug/outline', name: 'OutlineStudio', component: OutlineStudio },
    { path: '/book/:slug/review', name: 'CandidateReviewDesk', component: CandidateReviewDesk },
    { path: '/book/:slug/worldline', name: 'WorldlineRegeneration', component: WorldlineRegeneration },
    {
      path: '/debug/scheduler',
      name: 'CharacterSchedulerSimulator',
      component: CharacterSchedulerSimulator,
    },
  ],
})

export default router
