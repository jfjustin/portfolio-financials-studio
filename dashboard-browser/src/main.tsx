import {createRoot} from 'react-dom/client';
import Dashboard from './Dashboard';
const root=document.getElementById('portfolio-dashboard-root');
if(root)createRoot(root).render(<Dashboard/>);
