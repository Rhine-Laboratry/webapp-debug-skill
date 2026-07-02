<?php
namespace App\Controller\Admin;

class UsersController extends AppController
{
    public function index()
    {
        $this->set('users', $this->paginate());
        return $this->render('index');
    }

    public function delete($id)
    {
        $this->request->allowMethod(['post', 'delete']);
        $this->Flash->success('Deleted');
        return $this->redirect(['action' => 'index']);
    }

    public function exportCsv()
    {
        $this->request->allowMethod(['get']);
        return $this->response->withDownload('users.csv');
    }
}
