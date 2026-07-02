<?php
namespace Reports\Controller;

class ReportsController extends AppController
{
    public function index()
    {
        $this->set('reports', []);
    }
}
