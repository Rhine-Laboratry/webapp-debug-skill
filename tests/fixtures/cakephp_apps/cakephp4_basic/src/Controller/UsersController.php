<?php
namespace App\Controller;

class UsersController extends AppController
{
    public function index()
    {
        $users = $this->paginate($this->Users);
        $this->set(compact('users'));
    }

    public function add()
    {
        $this->request->allowMethod(['get', 'post']);
        if ($this->request->is('post')) {
            $this->Flash->success('Saved');
            return $this->redirect(['action' => 'index']);
        }
    }

    protected function helperMethod()
    {
    }

    private function privateAction()
    {
    }

    public function beforeFilter()
    {
    }

    public function _internal()
    {
    }
}
