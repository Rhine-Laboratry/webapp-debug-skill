<?php
namespace App\Model\Table;

class UsersTable
{
    public function validationDefault($validator)
    {
        return $validator->notEmptyString('name');
    }
}
