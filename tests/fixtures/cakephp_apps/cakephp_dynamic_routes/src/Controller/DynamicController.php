<?php
namespace App\Controller;

class DynamicController extends AppController
{
    public function view()
    {
        $this->set('item', []);
    }
}
